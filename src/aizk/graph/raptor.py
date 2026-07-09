import math
import uuid

import networkx as nx
from loguru import logger
from pgvector.utils import HalfVector
from sqlalchemy import delete, select

from ..config import settings
from ..extract import ontology
from ..store import (
    Community,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    acting_as,
)
from ..store.engine import bypass_rls, session
from .models import Node, RaptorReport
from .tier_builder import TierBuilder

# ontology.RAPTOR_SUMMARY is the entity type every tree node carries, so the recursive
# summaries live in the entity content table beside the knowledge nodes yet never mix with them.
# It is a structural catalog member, so the extractor's closed vocab never emits it, GraphWriter.
# resolve and the graph build never touch a summary node, and the whole tree is found and rebuilt
# by filtering entity content on this one tag.

# the predicate every parent-to-child tree edge carries. A part_of fact links a child summary to
# the summary one level above it, and it carries no embedding so it stays out of the knowledge-fact
# recall lanes, which all filter on a present embedding, and is read only as tree structure.
PART_OF = "part_of"

# phrasings that pin a query to a single detail rather than a theme, so a non-thematic query
# carrying one drops to the finest leaf summaries instead of a middle tier. Paired with the named
# entity count, they tell a pinned lookup from the mid-abstraction query landing between the two.
SPECIFICITY_MARKERS = (
    "specific",
    "detail",
    "exactly",
    "precisely",
    "definition of",
    "value of",
    "who is",
    "when did",
    "which exact",
)


def named_terms(query: str) -> int:
    """Count the capitalized words past the first, the proper nouns a query pins its detail to.

    Skips the leading word so a sentence-initial capital is not miscounted as a name, the same
    cheap proper-noun signal the query router reads, inlined here so the graph tier stays free of a
    retrieval import.

    query: the natural-language query whose named entities are counted.
    """
    return sum(1 for word in query.split()[1:] if word[:1].isupper())


def target_level(levels: list[int], query: str, thematic: bool) -> int:
    """The summary level a query reads, the root for a theme, the leaf for a detail, else a middle.

    A thematic query climbs to the broadest root summaries. A query carrying a specificity marker
    or two or more named entities drops to the finest leaf summaries just above the communities.
    Anything between reads the middle tier, so a mid-abstraction query lands on a mid-abstraction
    summary rather than snapping to either end. With only two levels the middle collapses onto
    the leaf.

    levels: the sorted summary levels above the leaves, ascending so the last is the broadest root.
    query: the natural-language query whose specificity picks the level.
    thematic: whether the query reads broad, taken from the router or the marker heuristic.
    """
    if thematic:
        return levels[-1]
    lowered = query.casefold()
    pinned = any(marker in lowered for marker in SPECIFICITY_MARKERS) or named_terms(query) >= 2
    return levels[0] if pinned else levels[(len(levels) - 1) // 2]


def to_floats(vector: HalfVector | list[float] | None) -> list[float]:
    """Narrow a stored embedding to a plain float list, unwrapping pgvector's HalfVector.

    The halfvec column reads back as a HalfVector the in-Python cosine cannot iterate, so the
    clustering reads its leaf embeddings through this, a vector already a list passing through. The
    caller has filtered to embedded rows, so the null the column type allows never reaches here.

    vector: the embedding as read from the database or as returned by the embedder.
    """
    assert vector is not None
    return vector.to_list() if isinstance(vector, HalfVector) else vector


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length dense vectors, zero when either has no magnitude.

    a: first dense vector.
    b: second dense vector.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def cluster(embeddings: list[list[float]], threshold: float) -> list[list[int]]:
    """Cluster summary embeddings into groups by greedy modularity over a similarity graph.

    Links two summaries when their cosine similarity clears the threshold, then runs the same
    networkx greedy modularity detection the community pass uses, so the tree is built with the
    house clustering rather than a new soft-assignment subsystem. The partition covers every node,
    so a summary that links to nothing comes back as its own singleton and is carried up unchanged.
    With no link clearing the threshold the graph is edgeless and each node is its own group, the
    signal the build loop reads to stop climbing.

    embeddings: the dense summary vectors of the current level, indexed by position.
    threshold: cosine similarity two summaries must reach to share an edge.
    """
    graph = nx.Graph()
    graph.add_nodes_from(range(len(embeddings)))
    for i in range(len(embeddings)):
        for j in range(i + 1, len(embeddings)):
            if cosine(embeddings[i], embeddings[j]) >= threshold:
                graph.add_edge(i, j)
    if graph.number_of_edges() == 0:
        return [[i] for i in range(len(embeddings))]
    groups = nx.community.greedy_modularity_communities(graph)
    return [sorted(group) for group in groups]


def redundant_parent(
    parents: list[tuple[Node, list[float]]], vector: list[float], threshold: float
) -> Node | None:
    """Return a parent already built this level whose summary near-duplicates a new one, else null.

    A fix, from the DTCRS paper, for plain RAPTOR's tendency to over-generate near-duplicate
    summaries. Soft clustering hands one child to several clusters, so two parents at a level end
    up saying nearly the same thing, and this finds the first existing parent within the
    redundancy threshold so the caller reuses it rather than write a second near-identical summary
    node, re-pointing the new cluster's children onto it.

    parents: the parents already decided for this level, each with its summary vector.
    vector: the candidate parent summary's vector.
    threshold: cosine similarity above which the candidate counts as a redundant duplicate.
    """
    for parent, parent_vector in parents:
        if cosine(parent_vector, vector) >= threshold:
            return parent
    return None


class RaptorTierBuilder(TierBuilder[list[Node], RaptorReport]):
    """One non-singleton cluster's structured-rollup pass, one climb of the RAPTOR tree.

    `result` is always set after `build`, the node every member's part_of edge points to. `content`
    is set only when a new summary was minted, the DTCRS prune against over-generation, so the
    level build knows which nodes still need writing once every cluster is done.

    members: the level-below nodes this cluster rolls up.
    level: the level number a newly minted summary content row is stamped with, in its claim.
    new_parents: the parents already minted this level, shared across every cluster's builder so
        the redundancy check compares a candidate against the whole level, not just this cluster.
    """

    def __init__(
        self,
        user_id: uuid.UUID,
        members: list[Node],
        level: int,
        new_parents: list[tuple[Node, list[float]]],
    ) -> None:
        super().__init__(user_id, settings.raptor_rollup_system, RaptorReport)
        self.members = members
        self.level = level
        self.new_parents = new_parents
        self.result: Node | None = None
        self.content: EntityContent | None = None

    async def gather(self) -> list[Node]:
        """This cluster's level-below members, already resolved by the level's own read."""
        return self.members

    def body(self, grounding: list[Node]) -> str:
        """Render the member labels and summaries as the structured call's user turn."""
        return "Child summaries:\n" + "\n".join(
            f"- {member.label}: {member.summary}" for member in grounding
        )

    def texts(self, report: RaptorReport) -> list[str]:
        """The one rollup summary this cluster's report carries."""
        return [report.summary]

    async def upsert(
        self, grounding: list[Node], report: RaptorReport, vectors: list[list[float]]
    ) -> int:
        """Reuse a near-duplicate parent already minted this level, else stage a fresh one."""
        vector = vectors[0]
        parent = redundant_parent(self.new_parents, vector, settings.raptor_redundancy_threshold)
        if parent is None:
            parent = Node(
                entity_id=uuid.uuid7(),
                label=report.label,
                summary=report.summary,
                embedding=list(vector),
            )
            self.content = EntityContent(
                id=parent.entity_id,
                name=report.label,
                type=ontology.RAPTOR_SUMMARY,
                embedding=vector,
            )
            self.new_parents.append((parent, list(vector)))
        self.result = parent
        return 1 if self.content is not None else 0


def part_of_content(child_id: uuid.UUID, parent: Node) -> FactContent:
    """Build one part_of tree edge's content from a child summary entity to its parent.

    The edge carries no embedding so it never enters the knowledge-fact recall lanes, which all
    require a present embedding, and is read only as structure.

    child_id: the lower-level summary entity the edge starts from.
    parent: the higher-level summary node the edge points to.
    """
    return FactContent(
        subject_id=child_id,
        object_id=parent.entity_id,
        predicate=PART_OF,
        statement=f"is part of {parent.label}",
    )


def part_of_claim(user_id: uuid.UUID, content_id: uuid.UUID) -> FactClaim:
    """Build one part_of tree edge's claim, owned by the user that built the tree.

    The default open `recorded` keeps it inside the same bi-temporal shape every claim has, so the
    read-path validity gate treats it like any current edge while it lives, and a rebuild clears
    the whole tree before writing a new one. The tree carries no scope of its own, always private
    to the user that built it.

    user_id: identity that owns the edge.
    content_id: the part_of fact content this claim stakes.
    """
    return FactClaim(
        content_id=content_id,
        owner_id=user_id,
    )


def summary_claim(
    user_id: uuid.UUID, content: EntityContent, level: int, summary: str
) -> EntityClaim:
    """One freshly minted RAPTOR summary's claim, stamped with its tree level and own text.

    user_id: identity that owns the tree.
    content: the summary's own freshly minted entity content.
    level: the level number this summary was rolled up to.
    summary: the summary's own paragraph, duplicated onto the claim so recall reads it with no
        extra join back to content.
    """
    return EntityClaim(
        content_id=content.id,
        owner_id=user_id,
        attributes={"level": level, "summary": summary},
    )


async def rolled_up_parent(
    user_id: uuid.UUID,
    members: list[Node],
    level: int,
    new_parents: list[tuple[Node, list[float]]],
) -> tuple[Node, EntityContent | None]:
    """A non-singleton cluster's parent node, and its freshly minted content when this cluster
    itself wrote one rather than reusing a redundant parent an earlier cluster this level minted.

    user_id: identity that owns the written summary content and claim.
    members: this cluster's level-below members, at least two.
    level: the level number a newly minted summary is stamped with.
    new_parents: the parents already minted this level, shared across every cluster's builder.
    """
    builder = RaptorTierBuilder(user_id, members, level, new_parents)
    await builder.build()
    assert builder.result is not None  # a non-singleton cluster always rolls up to a parent
    return builder.result, builder.content


async def write_level(
    user_id: uuid.UUID,
    contents: list[EntityContent],
    claims: list[EntityClaim],
    edges: list[tuple[uuid.UUID, Node]],
) -> None:
    """Write one level's freshly minted summary content, claims, and part_of edges, in one go.

    `FactContent.id` is client-generated (`Id.id`'s `default_factory=uuid.uuid7`), already
    populated the moment the object is constructed, so every claim below already knows the content
    id it stakes with no round trip to flush it first. The flush between the two `add_all` calls is
    still required, content and claim share no ORM `relationship()` for SQLAlchemy's unit-of-work
    to auto-order the insert on, only a bare FK column, so a claim added in the same batch as its
    content can flush ahead of it and violate the FK.

    user_id: identity that owns every row written.
    contents: this level's freshly minted summary content, empty when every cluster reused a
        redundant parent or was a singleton.
    claims: this level's freshly minted summary claims, one per entry in contents.
    edges: every member-to-parent part_of edge this level's clusters resolved, new or reused alike.
    """
    async with acting_as(user_id):
        edge_contents = [part_of_content(child_id, parent) for child_id, parent in edges]
        session().add_all(contents)
        session().add_all(edge_contents)
        await session().flush()
        session().add_all(claims)
        session().add_all(
            part_of_claim(user_id, edge_content.id) for edge_content in edge_contents
        )


async def build_level(
    user_id: uuid.UUID,
    nodes: list[Node],
    clusters: list[list[int]],
    level: int,
) -> tuple[list[Node], int]:
    """Build one tree level from a clustering of the level below, return its nodes and write count.

    A singleton cluster is carried up unchanged, the prune that keeps RAPTOR from minting a summary
    node that just restates one child. A multi-member cluster's parent comes from rolled_up_parent,
    which rolls it up, embeds it outside any transaction, and either reuses a redundant parent
    already minted this level or stages a fresh summary entity content plus this user's own
    claim on it. Every member of a built cluster then gets a part_of edge to its parent regardless
    of whether the parent was freshly minted or reused. write_level then writes everything staged
    in one owner-scoped transaction, so a slow rollup never holds a write lock.

    user_id: identity that owns the written summary content, claims, and part_of edges.
    nodes: the level-below nodes the clusters index into.
    clusters: the grouping of the level-below nodes into this level's parents.
    level: the level number stamped on every summary claim this call writes.
    """
    next_nodes: list[Node] = []
    new_parents: list[tuple[Node, list[float]]] = []
    contents: list[EntityContent] = []
    claims: list[EntityClaim] = []
    edges: list[tuple[uuid.UUID, Node]] = []
    for group in clusters:
        members = [nodes[index] for index in group]
        if len(members) == 1:
            next_nodes.append(members[0])
            continue
        parent, content = await rolled_up_parent(user_id, members, level, new_parents)
        if content is not None:
            contents.append(content)
            claims.append(summary_claim(user_id, content, level, parent.summary))
            next_nodes.append(parent)
        edges.extend((member.entity_id, parent) for member in members)
    await write_level(user_id, contents, claims, edges)
    logger.info(
        "raptor level {} built {} summaries over {} nodes", level, len(contents), len(nodes)
    )
    return next_nodes, len(contents)


async def stale_tree_content(user_id: uuid.UUID) -> list[uuid.UUID]:
    """This user's own prior RAPTOR tree content ids, read ahead of a rebuild.

    user_id: identity whose prior tree, if any, is found.
    """
    async with acting_as(user_id):
        return list(
            await session().scalars(
                select(EntityClaim.content_id)
                .join(EntityContent, EntityContent.id == EntityClaim.content_id)
                .where(
                    EntityClaim.owner_id == user_id,
                    EntityContent.type == ontology.RAPTOR_SUMMARY,
                )
            )
        )


async def clear_stale_tree(user_id: uuid.UUID) -> None:
    """Delete a user's prior RAPTOR tree content wholesale, so a rebuild is idempotent.

    Content carries no owner of its own and no ordinary DELETE policy at all, so this runs on the
    owner-role admin connection, bypassing row level security entirely. The delete cascades its
    claims and part_of edges away through their foreign keys.

    user_id: identity whose prior tree, if any, is cleared.
    """
    stale = await stale_tree_content(user_id)
    if not stale:
        return
    async with bypass_rls() as session:
        await session.execute(delete(EntityContent).where(EntityContent.id.in_(stale)))
        await session.commit()


def leaf_content(community: Community) -> EntityContent:
    """One community's level-0 leaf entity content, ready to mint.

    community: the single-level community this leaf summarizes, already known embedded.
    """
    return EntityContent(
        id=uuid.uuid7(),
        name=community.label,
        type=ontology.RAPTOR_SUMMARY,
        embedding=community.embedding,
    )


def leaf_claim(user_id: uuid.UUID, leaf: EntityContent, community: Community) -> EntityClaim:
    """One level-0 leaf's claim, its attributes carrying a backreference to its own community.

    user_id: identity that owns the tree.
    leaf: the leaf's own freshly minted entity content.
    community: the community this leaf summarizes.
    """
    return EntityClaim(
        content_id=leaf.id,
        owner_id=user_id,
        attributes={"level": 0, "summary": community.summary, "community": str(community.id)},
    )


async def leaf_nodes(user_id: uuid.UUID) -> list[Node]:
    """Clear any prior tree and mint the level-0 leaves, one summary entity per community.

    Plain RAPTOR builds its tree over text chunks, but here the leaves are the single-level
    communities the global lane already detected, so the recursive summaries climb above them.
    Returns the leaves, empty when fewer than two communities exist to cluster.

    user_id: identity that owns the tree and whose visibility scopes the communities read.
    """
    await clear_stale_tree(user_id)
    async with acting_as(user_id):
        communities = list(
            await session().scalars(select(Community).where(Community.embedding.is_not(None)))
        )
        if len(communities) < 2:
            return []
        leaves = [leaf_content(community) for community in communities]
        session().add_all(leaves)
        # content and claim share no ORM `relationship()`, only a bare FK column, so the leaves
        # must actually flush before the matching claims are added, the same ordering
        # `write_level` observes, or a claim can insert ahead of the content row it stakes.
        await session().flush()
        session().add_all(
            leaf_claim(user_id, leaf, community)
            for leaf, community in zip(leaves, communities, strict=True)
        )
    return [
        Node(
            entity_id=leaf.id,
            label=community.label,
            summary=community.summary,
            embedding=to_floats(community.embedding),
        )
        for leaf, community in zip(leaves, communities, strict=True)
    ]


async def build_raptor(
    user_id: uuid.UUID | None = None,
) -> int:
    """Build the recursive summary tree above the communities, return how many summaries it wrote.

    Mints the level-0 leaves from the communities, then climbs one level at a time, clustering the
    current level's summaries and rolling each cluster of two or more into a summary-of-summaries
    while carrying singletons up unchanged. It stops once a level holds at most raptor_root_max
    nodes, once a clustering merges nothing so the tree cannot shrink further, or once
    raptor_max_levels is reached, so the climb always terminates at a small root set. Returns the
    count of summary entities written across the levels above the leaves.

    user_id: identity that owns the written tree, the system user when null.
    """
    user_id = user_id or settings.system_user_id
    nodes = await leaf_nodes(user_id)
    if not nodes:
        return 0
    written = 0
    level = 1
    while len(nodes) > settings.raptor_root_max and level <= settings.raptor_max_levels:
        clusters = cluster([node.embedding for node in nodes], settings.raptor_sim_threshold)
        if len(clusters) >= len(nodes):
            break
        nodes, count = await build_level(user_id, nodes, clusters, level)
        written += count
        level += 1
    logger.info("raptor tree wrote {} summaries under user {}", written, user_id)
    return written


async def raptor_levels() -> list[int]:
    """The sorted summary levels above the leaves, the levels recall can retrieve from.

    Reads the distinct level tags of the visible summary claims and keeps those above the level-0
    leaves, so recall knows which level answers a broad query and which a pointed one. Empty until
    a tree has been built.
    """
    depth = EntityClaim.attributes["level"].as_integer()
    rows = await session().scalars(
        select(depth)
        .join(EntityContent, EntityContent.id == EntityClaim.content_id)
        .where(EntityContent.type == ontology.RAPTOR_SUMMARY, depth >= 1)
        .distinct()
    )
    return sorted(rows)


async def raptor_search(
    query: str,
    vector: list[float],
    thematic: bool = True,
    k: int = 3,
) -> list[tuple[str, str, int, float]]:
    """Rank the level-appropriate summaries against a query, broad to root, pointed to the leaf.

    A broad, thematic query reads the highest tree level, the few root summaries that each fold a
    whole area into one paragraph, a pinned query reads the lowest summary level above the
    communities, the finer summaries nearer the facts, and a mid-abstraction query in between reads
    a middle tier. Returns the top k as label, summary, level, and a similarity score, the lane
    recall folds in beside the community summaries. Takes the caller's own open session and an
    already-embedded query vector rather than opening a session or embedding of its own, since
    recall's one round already holds both and a second session here would open a second connection
    nested inside the first for no reason.

    query: natural-language query, read only for its specificity when the round is not thematic.
    vector: dense query embedding.
    thematic: whether the query is broad, reading the root level rather than the leaf summaries.
    k: number of summaries to return.
    """
    levels = await raptor_levels()
    if not levels:
        return []
    level = target_level(levels, query, thematic)
    distance = EntityContent.embedding.cosine_distance(vector)
    depth = EntityClaim.attributes["level"].as_integer()
    summary = EntityClaim.attributes["summary"].astext
    rows = await session().execute(
        select(EntityContent.name, summary, distance.label("distance"))
        .join(EntityClaim, EntityClaim.content_id == EntityContent.id)
        .where(
            EntityContent.type == ontology.RAPTOR_SUMMARY,
            depth == level,
            EntityContent.embedding.is_not(None),
        )
        .order_by(distance)
        .limit(k)
    )
    return [(row.name, row[1], level, 1.0 - row.distance) for row in rows]
