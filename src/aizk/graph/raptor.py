import asyncio
import math
from itertools import batched

from loguru import logger
from mainboard.profiling import span
from networkx.algorithms.community.modularity_max import greedy_modularity_communities
from networkx.classes import Graph
from patos import sql
from pgvector import HalfVector
from pydantic import UUID5
from sqlalchemy import Integer, column, delete
from sqlmodel import select

from ..config import settings
from ..ontology import System
from ..serving.embed import embed
from ..serving.extract import LLM
from ..store import Community, Entity, Fact
from ..store.identity import User
from ..types import Scopes
from .ids import entity_id, fact_id
from .models import Node, RaptorReport

_PART_OF = "part_of"


def to_floats(vector: HalfVector | list[float] | None) -> list[float]:
    """Materialize a stored embedding as ordinary float values."""
    assert vector is not None
    return vector.to_list() if isinstance(vector, HalfVector) else vector


def cosine(a: list[float], b: list[float]) -> float:
    """Return cosine similarity, or zero when either vector has no magnitude."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def modularity_groups(graph: Graph) -> list[list[int]]:
    """Partition one prepared similarity graph, preserving isolated nodes."""
    if graph.number_of_edges() == 0:
        return [[index] for index in graph.nodes]
    with span("raptor_modularity", memory=True):
        return [sorted(group) for group in greedy_modularity_communities(graph)]


def redundant_parent(
    parents: list[tuple[Node, list[float]]], vector: list[float], threshold: float
) -> Node | None:
    """Return an already-staged parent whose summary near-duplicates a new one."""
    return next(
        (
            parent
            for parent, parent_vector in parents
            if cosine(parent_vector, vector) >= threshold
        ),
        None,
    )


class RaptorBuilder:
    """Plan a complete RAPTOR generation before its atomic database replacement."""

    __slots__ = ("claims", "contents", "edge_claims", "edges", "llm", "scopes")

    def __init__(self, scopes: Scopes) -> None:
        self.scopes = frozenset(scopes)
        self.contents: list[Entity.Content] = []
        self.claims: list[Entity.Claim] = []
        self.edges: list[Fact.Content] = []
        self.edge_claims: list[Fact.Claim] = []
        self.llm = LLM.configured()

    def claim(self, content: Entity.Content, level: int, summary: str) -> Entity.Claim:
        """Build one exact-scope summary claim."""
        return Entity.Claim(
            content_id=content.id,
            created_by=settings.system_user_id,
            scopes=sorted(self.scopes),
            attributes={"level": level, "summary": summary},
        )

    def leaves(self, communities: list[Community]) -> list[Node]:
        """Stage one level-zero summary entity for every community."""
        nodes: list[Node] = []
        for community in communities:
            content = Entity.Content(
                id=entity_id(community.label, System.Entity.RAPTOR_SUMMARY),
                name=community.label,
                type=System.Entity.RAPTOR_SUMMARY,
                embedding=community.embedding,
            )
            claim = self.claim(content, 0, community.summary)
            claim.attributes["community"] = str(community.id)
            self.contents.append(content)
            self.claims.append(claim)
            nodes.append(
                Node(
                    entity_id=content.id,
                    label=community.label,
                    summary=community.summary,
                    embedding=to_floats(community.embedding),
                )
            )
        return nodes

    def connect(self, members: list[Node], parent: Node) -> None:
        """Stage part-of fact content and claims from each child to one parent."""
        for member in members:
            edge = Fact.Content(
                id=fact_id(
                    member.entity_id,
                    _PART_OF,
                    parent.entity_id,
                    f"is part of {parent.label}",
                ),
                subject_id=member.entity_id,
                object_id=parent.entity_id,
                predicate=_PART_OF,
                statement=f"is part of {parent.label}",
            )
            self.edges.append(edge)
            self.edge_claims.append(
                Fact.Claim(
                    content_id=edge.id,
                    created_by=settings.system_user_id,
                    scopes=sorted(self.scopes),
                )
            )

    async def similarity_groups(self, embeddings: list[list[float]]) -> list[list[int]]:
        """Build a similarity graph with PostgreSQL vector comparisons."""
        graph = Graph()
        graph.add_nodes_from(range(len(embeddings)))
        if len(embeddings) < 2:
            return modularity_groups(graph)
        vectors = sql.relation(
            "raptor_vector",
            (
                column("ordinal", Integer),
                column("embedding", sql.CosineHalfvec(settings.embed_dim)),
            ),
            list(enumerate(embeddings)),
        )
        left = vectors.alias("raptor_left")
        right = vectors.alias("raptor_right")
        distance = left.c.embedding @ right.c.embedding
        with span("raptor_similarity_query", memory=True):
            async with User.system(self.scopes) as session:
                pairs = await session.exec(
                    select(left.c.ordinal, right.c.ordinal)
                    .select_from(left.join(right, left.c.ordinal < right.c.ordinal))
                    .where(distance <= 1.0 - settings.raptor_sim_threshold)
                )
                graph.add_edges_from(pairs)
        return modularity_groups(graph)

    async def parent(
        self,
        members: list[Node],
        level: int,
        parents: list[tuple[Node, list[float]]],
    ) -> tuple[Node, bool]:
        """Summarize one cluster and stage a new parent unless this level already has it."""
        with span("raptor_summary", memory=True):
            report = await self.llm.generate(
                settings.raptor_rollup_system,
                "Child summaries:\n"
                + "\n".join(
                    f"- {member.label}: {member.summary[: settings.raptor_child_summary_chars]}"
                    for member in members
                ),
                RaptorReport,
            )
        with span("raptor_embedding", memory=True):
            [vector] = await embed([report.summary], mode="document")
        parent = redundant_parent(parents, vector, settings.raptor_redundancy_threshold)
        created = parent is None
        if parent is None:
            content = Entity.Content(
                id=entity_id(report.label, System.Entity.RAPTOR_SUMMARY),
                name=report.label,
                type=System.Entity.RAPTOR_SUMMARY,
                embedding=vector,
            )
            parent = Node(
                entity_id=content.id,
                label=report.label,
                summary=report.summary,
                embedding=vector,
            )
            self.contents.append(content)
            self.claims.append(self.claim(content, level, report.summary))
            parents.append((parent, vector))
        self.connect(members, parent)
        return parent, created

    async def level(
        self, nodes: list[Node], groups: list[list[int]], level: int
    ) -> tuple[list[Node], int]:
        """Plan one tree level and keep reused parents in the next level exactly once."""
        next_nodes: list[Node] = []
        next_ids: set[UUID5] = set()
        parents: list[tuple[Node, list[float]]] = []
        written = 0
        for group_batch in batched(groups, settings.raptor_build_concurrency, strict=False):
            member_groups = [[nodes[index] for index in group] for group in group_batch]
            generated = iter(
                await asyncio.gather(
                    *(
                        self.parent(members, level, parents)
                        for members in member_groups
                        if len(members) > 1
                    )
                )
            )
            for members in member_groups:
                parent, created = (members[0], False) if len(members) == 1 else next(generated)
                if parent.entity_id not in next_ids:
                    next_nodes.append(parent)
                    next_ids.add(parent.entity_id)
                written += created
        return next_nodes, written

    async def build(self, communities: list[Community]) -> int:
        """Plan all levels and return the number of non-leaf summaries staged."""
        if len(communities) < 2:
            return 0
        nodes = self.leaves(communities)
        written = 0
        level = 1
        while len(nodes) > settings.raptor_root_max and level <= settings.raptor_max_levels:
            with span("raptor_clustering", memory=True):
                groups = await self.similarity_groups([node.embedding for node in nodes])
                groups = [
                    list(branch)
                    for group in groups
                    for branch in batched(group, settings.raptor_branch_factor, strict=False)
                ]
            if len(groups) >= len(nodes):
                break
            nodes, count = await self.level(nodes, groups, level)
            written += count
            level += 1
        return written

    async def replace(self, stale: list[UUID5]) -> None:
        """Atomically delete the stale generation and insert the complete staged one."""
        async with User.system().owner as opened:
            if stale:
                await opened.exec(delete(Entity.Content).where(Entity.Content.id.in_(stale)))
            opened.add_all(self.contents)
            await opened.flush()
            opened.add_all(self.claims)
            opened.add_all(self.edges)
            await opened.flush()
            opened.add_all(self.edge_claims)


async def build_raptor(
    scopes: Scopes | None = None,
) -> int:
    """Build and atomically replace one exact scope's recursive summary tree."""
    key = frozenset(scopes or (settings.system_user_id,))
    builder = RaptorBuilder(key)
    with span("raptor_snapshot", memory=True):
        async with User.system(key) as session:
            communities = list(
                await session.exec(
                    select(Community).where(
                        Community.embedding.is_not(None),
                        Community.scopes == sorted(key),
                    )
                )
            )
            stale = list(
                await session.exec(
                    select(Entity.Claim.content_id)
                    .join(Entity.Content, Entity.Content.id == Entity.Claim.content_id)
                    .where(
                        Entity.Claim.scopes == sorted(key),
                        Entity.Content.type == System.Entity.RAPTOR_SUMMARY,
                    )
                )
            )
    with span("raptor_planning", memory=True):
        written = await builder.build(communities)
    with span("raptor_replacement", memory=True):
        await builder.replace(stale)
    logger.info("raptor tree wrote {} summaries in scope {}", written, key)
    return written
