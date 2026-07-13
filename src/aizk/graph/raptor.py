import math
import uuid

from loguru import logger
from networkx.algorithms.community.modularity_max import greedy_modularity_communities
from networkx.classes import Graph
from pgvector import HalfVector
from sqlalchemy import delete
from sqlmodel import select

from ..config import settings
from ..extract import ontology
from ..extract.llm import structured
from ..serving import embed
from ..store import Community, EntityClaim, EntityContent, FactClaim, FactContent
from ..store.engine import bypass_rls
from ..store.identity import User
from ..types import Scopes
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


def cluster(embeddings: list[list[float]], threshold: float) -> list[list[int]]:
    """Cluster summary embeddings by greedy modularity over a similarity graph."""
    graph = Graph()
    graph.add_nodes_from(range(len(embeddings)))
    graph.add_edges_from(
        (left, right)
        for left in range(len(embeddings))
        for right in range(left + 1, len(embeddings))
        if cosine(embeddings[left], embeddings[right]) >= threshold
    )
    if graph.number_of_edges() == 0:
        return [[index] for index in range(len(embeddings))]
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

    __slots__ = ("claims", "contents", "edge_claims", "edges", "embedder", "scopes")

    def __init__(self, scopes: Scopes) -> None:
        self.scopes = frozenset(scopes)
        self.contents: list[EntityContent] = []
        self.claims: list[EntityClaim] = []
        self.edges: list[FactContent] = []
        self.edge_claims: list[FactClaim] = []

    def claim(self, content: EntityContent, level: int, summary: str) -> EntityClaim:
        """Build one exact-scope summary claim."""
        return EntityClaim(
            content_id=content.id,
            created_by=settings.system_user_id,
            scopes=sorted(self.scopes),
            attributes={"level": level, "summary": summary},
        )

    def leaves(self, communities: list[Community]) -> list[Node]:
        """Stage one level-zero summary entity for every community."""
        nodes: list[Node] = []
        for community in communities:
            content = EntityContent(
                name=community.label,
                type=ontology.RAPTOR_SUMMARY,
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
            edge = FactContent(
                subject_id=member.entity_id,
                object_id=parent.entity_id,
                predicate=_PART_OF,
                statement=f"is part of {parent.label}",
            )
            self.edges.append(edge)
            self.edge_claims.append(
                FactClaim(
                    content_id=edge.id,
                    created_by=settings.system_user_id,
                    scopes=sorted(self.scopes),
                )
            )

    async def parent(
        self,
        members: list[Node],
        level: int,
        parents: list[tuple[Node, list[float]]],
    ) -> tuple[Node, bool]:
        """Summarize one cluster and stage a new parent unless this level already has it."""
        report = await structured(
            settings.raptor_rollup_system,
            "Child summaries:\n"
            + "\n".join(f"- {member.label}: {member.summary}" for member in members),
            RaptorReport,
        )
        [vector] = await embed([report.summary], mode="document")
        parent = redundant_parent(parents, vector, settings.raptor_redundancy_threshold)
        created = parent is None
        if parent is None:
            content = EntityContent(
                name=report.label,
                type=ontology.RAPTOR_SUMMARY,
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
        next_ids: set[uuid.UUID] = set()
        parents: list[tuple[Node, list[float]]] = []
        written = 0
        for group in groups:
            members = [nodes[index] for index in group]
            if len(members) == 1:
                parent, created = members[0], False
            else:
                parent, created = await self.parent(members, level, parents)
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
            groups = cluster([node.embedding for node in nodes], settings.raptor_sim_threshold)
            if len(groups) >= len(nodes):
                break
            nodes, count = await self.level(nodes, groups, level)
            written += count
            level += 1
        return written

    async def replace(self, stale: list[uuid.UUID]) -> None:
        """Atomically delete the stale generation and insert the complete staged one."""
        async with bypass_rls() as opened:
            if stale:
                await opened.exec(delete(EntityContent).where(EntityContent.id.in_(stale)))
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
                select(EntityClaim.content_id)
                .join(EntityContent, EntityContent.id == EntityClaim.content_id)
                .where(
                    EntityClaim.scopes == sorted(key),
                    EntityContent.type == ontology.RAPTOR_SUMMARY,
                )
            )
        )
    builder = RaptorBuilder(key)
    written = await builder.build(communities)
    await builder.replace(stale)
    logger.info("raptor tree wrote {} summaries in scope {}", written, key)
    return written
