import uuid

import networkx as nx
from loguru import logger
from sqlalchemy import select

from ..config import settings
from ..serving import Embedder
from ..store import Community, EntityContent, LiveFact, acting_as
from .models import CommunitySummary
from .tier_builder import TierBuilder


def detect(
    facts: list[LiveFact], min_size: int, backend: str = "networkx"
) -> list[set[uuid.UUID]]:
    """Detect entity communities over the latest-fact graph by Louvain modularity.

    Builds an undirected graph from the binary facts, each a subject to object edge, then runs the
    networkx Louvain detection with a fixed seed for a deterministic partition, keeping the
    communities of at least min_size entities. Louvain is the exact algorithm nx-cugraph
    accelerates, so passing a registered backend like cugraph flips detection onto the GPU with no
    code change.

    facts: the latest binary facts whose subject and object define the edges.
    min_size: smallest entity count a community must reach to be kept.
    backend: networkx graph backend to run Louvain on, the in-process networkx by default or a
        registered accelerator like cugraph for a GPU tier.
    """
    graph = nx.Graph()
    graph.add_edges_from(
        (fact.subject_id, fact.object_id) for fact in facts if fact.object_id is not None
    )
    if graph.number_of_edges() == 0:
        return []
    # the in-process default and a registered accelerator take different dispatch paths, so the
    # backend keyword is passed only when one is named and omitted entirely for plain networkx.
    if backend == "networkx":
        communities = nx.community.louvain_communities(graph, seed=settings.louvain_seed)
    else:
        communities = nx.community.louvain_communities(
            graph, seed=settings.louvain_seed, backend=backend
        )
    return [set(members) for members in communities if len(members) >= min_size]


Grounding = tuple[list[str], list[str]]


class CommunityTierBuilder(TierBuilder[Grounding, CommunitySummary]):
    """One cluster's structured-summary pass, the GraphRAG community report in miniature.

    cluster: the entity ids Louvain grouped together, the community this instance summarizes.
    entities: every visible entity keyed by id, the roster the cluster's member names read off.
    facts: every visible embedded fact, the pool this cluster's member statements filter from.
    """

    def __init__(
        self,
        principal_id: uuid.UUID,
        cluster: set[uuid.UUID],
        entities: dict[uuid.UUID, EntityContent],
        facts: list[LiveFact],
    ) -> None:
        super().__init__(principal_id, settings.community_summary_system, CommunitySummary)
        self.cluster = cluster
        self.entities = entities
        self.facts = facts

    async def gather(self) -> Grounding:
        """The cluster's member entity names and the fact statements among them."""
        names = [self.entities[member].name for member in self.cluster if member in self.entities]
        statements = [
            fact.statement
            for fact in self.facts
            if fact.subject_id in self.cluster or fact.object_id in self.cluster
        ]
        return names, statements

    def body(self, grounding: Grounding) -> str:
        """Render the cluster's roster and facts as the structured call's user turn."""
        names, statements = grounding
        roster = "Entities: " + ", ".join(names)
        facts = "Facts:\n" + "\n".join(f"- {statement}" for statement in statements)
        return f"{roster}\n\n{facts}"

    def texts(self, report: CommunitySummary) -> list[str]:
        """The one summary paragraph this cluster's report carries."""
        return [report.summary]

    async def upsert(
        self, grounding: Grounding, report: CommunitySummary, vectors: list[list[float]]
    ) -> int:
        """Store the cluster's summary as a new, scoped community row."""
        async with acting_as(self.principal_id) as session:
            session.add(
                Community(
                    owner_id=self.principal_id,
                    label=report.label,
                    summary=report.summary,
                    embedding=vectors[0],
                    member_ids=list(self.cluster),
                )
            )
        logger.info("summarized community {!r} of {} entities", report.label, len(self.cluster))
        return 1


async def build_communities(
    principal_id: uuid.UUID | None = None,
) -> int:
    """Detect communities over the entity graph, summarize each, store the rows, return the count.

    Loads the visible entities and latest facts once, detects the communities of at least
    settings.community_min_size entities, and runs one CommunityTierBuilder per cluster, each
    summarizing its own entities and facts with the LLM outside any transaction, embedding the
    summary, and storing its own scoped community row. Committing one community at a time mirrors
    build_graph so a slow summarization never holds a write lock and the build is resumable.

    principal_id: identity that owns the written communities, the system principal when null.
    """
    principal_id = principal_id or settings.system_principal_id
    async with acting_as(principal_id) as session:
        entities = {entity.id: entity for entity in await session.scalars(select(EntityContent))}
        # only embedded knowledge facts define the cluster graph, so the structural part_of edges
        # of the RAPTOR tree, which carry no embedding, never form their own summary communities.
        # `live_fact` already carries the current-and-reviewed gate.
        facts = list(
            await session.scalars(select(LiveFact).where(LiveFact.embedding.is_not(None)))
        )
    clusters = detect(facts, settings.community_min_size, settings.community_backend)
    written = 0
    for cluster in clusters:
        written += await CommunityTierBuilder(principal_id, cluster, entities, facts).build()
    return written


async def community_search(
    query: str,
    principal_id: uuid.UUID | None = None,
    k: int = 3,
    scope: uuid.UUID | None = None,
) -> list[tuple[str, str, float]]:
    """Rank stored community summaries against a query, return the closest as label and summary.

    Embeds the query and ranks the row-level-security-visible communities by cosine distance to
    their summary embedding, returning the top k as label, summary, and a similarity score. This is
    the global lane recall folds in when a query reads thematic rather than pointed.

    query: natural-language thematic query.
    principal_id: identity whose row level security visibility scopes the communities, the system
        principal when null.
    k: number of community summaries to return.
    scope: group id narrowing the read to that group's composed graph, the whole visible union
        when null.
    """
    principal_id = principal_id or settings.system_principal_id
    embedder = Embedder()
    [vector] = await embedder.embed([query], mode="query")
    distance = Community.embedding.cosine_distance(vector)
    async with acting_as(principal_id, scope) as session:
        rows = await session.execute(
            select(Community.label, Community.summary, distance.label("distance"))
            .where(Community.embedding.is_not(None))
            .order_by(distance)
            .limit(k)
        )
        return [(row.label, row.summary, 1.0 - row.distance) for row in rows]
