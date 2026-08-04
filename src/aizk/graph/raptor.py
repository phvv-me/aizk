import asyncio
import math
from collections.abc import Callable, Iterable
from itertools import batched
from typing import cast

from loguru import logger
from mainboard.profiling import span
from networkx.algorithms.community.modularity_max import greedy_modularity_communities
from networkx.classes import Graph
from patos import FlexModel, sql
from pgvector import HalfVector, Vector
from pydantic import UUID5, Field
from sqlalchemy import Integer, column, delete, or_
from sqlmodel import select

from ..config import settings
from ..ontology import System
from ..serving.embed import Embedder
from ..serving.extract import LLM
from ..store import Community, Entity, Fact
from ..store.identity import User
from ..store.locking import acquire_locks
from ..store.models.tables import EntityClaim, EntityContent, FactClaim, FactContent
from ..store.vector import CosineVector, cosine_distance
from ..types import Scopes
from .ids import entity_id, fact_id
from .models import Node, RaptorReport

_PART_OF = "part_of"

# SQLModel synthesizes table-model keyword constructors outside the static signatures.
_entity_content = cast("Callable[..., EntityContent]", Entity.Content)
_fact_content = cast("Callable[..., FactContent]", Fact.Content)


def to_floats(vector: HalfVector | Vector | list[float] | None) -> list[float]:
    """Materialize a stored embedding as ordinary float values."""
    assert vector is not None
    return vector.to_list() if isinstance(vector, (HalfVector, Vector)) else vector


def cosine(a: list[float], b: list[float]) -> float:
    """Return cosine similarity, or zero when either vector has no magnitude."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def modularity_groups(graph: Graph[int]) -> list[list[int]]:
    """Partition one prepared similarity graph, preserving isolated nodes."""
    if graph.number_of_edges() == 0:
        return [[index] for index in graph.nodes]
    with span("raptor_modularity"):
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


class RaptorBuilder(FlexModel):
    """Plan a complete RAPTOR generation before its atomic database replacement."""

    scopes: Scopes
    llm: LLM
    embed: Embedder
    contents: list[EntityContent] = Field(default_factory=list)
    claims: list[EntityClaim] = Field(default_factory=list)
    edges: list[FactContent] = Field(default_factory=list)
    edge_claims: list[FactClaim] = Field(default_factory=list)

    def claim(self, content: EntityContent, level: int, summary: str) -> EntityClaim:
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
            content = _entity_content(
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
            edge = _fact_content(
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
        graph: Graph[int] = Graph()
        graph.add_nodes_from(range(len(embeddings)))
        if len(embeddings) < 2:
            return modularity_groups(graph)
        vectors = sql.relation(
            "raptor_vector",
            (
                column("ordinal", Integer),
                column("embedding", CosineVector(settings.embed_dim)),
            ),
            list(enumerate(embeddings)),
        )
        left = vectors.alias("raptor_left")
        right = vectors.alias("raptor_right")
        distance = cosine_distance(left.c.embedding, right.c.embedding)
        with span("raptor_similarity_query"):
            async with User.system(self.scopes) as session:
                pairs = await session.exec(
                    select(left.c.ordinal, right.c.ordinal)
                    .select_from(left.join(right, left.c.ordinal < right.c.ordinal))
                    .where(distance <= 1.0 - settings.raptor_sim_threshold)
                )
                graph.add_edges_from(cast("Iterable[tuple[int, int]]", pairs))
        return modularity_groups(graph)

    async def parent(
        self,
        members: list[Node],
        level: int,
        parents: list[tuple[Node, list[float]]],
    ) -> tuple[Node, bool]:
        """Summarize one cluster and stage a new parent unless this level already has it."""
        with span("raptor_summary"):
            report = await self.llm.generate(
                settings.raptor_rollup_system,
                "Child summaries:\n"
                + "\n".join(
                    f"- {member.label}: {member.summary[: settings.raptor_child_summary_chars]}"
                    for member in members
                ),
                RaptorReport,
            )
        with span("raptor_embedding"):
            [vector] = await self.embed.embed([report.summary], mode="document")
        parent = redundant_parent(parents, vector, settings.raptor_redundancy_threshold)
        created = parent is None
        if parent is None:
            content = _entity_content(
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
            with span("raptor_clustering"):
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

    async def replace(self) -> None:
        """Replace one scope's generation atomically while sharing content across scopes.

        A transaction-scoped advisory lock keyed by the canonical scope serializes
        concurrent builds, and the stale generation is reselected under that lock so a
        racing build can never resurrect or double-delete another generation's rows.
        """
        scope_list = sorted(self.scopes)
        async with User.system().owner as opened:
            await acquire_locks(
                opened,
                ["raptor|" + ",".join(str(scope) for scope in scope_list)],
            )
            stale = list(
                await opened.exec(
                    select(Entity.Claim.content_id)
                    .join(Entity.Content, Entity.Content.id == Entity.Claim.content_id)
                    .where(
                        Entity.Claim.scopes == scope_list,
                        Entity.Content.type == System.Entity.RAPTOR_SUMMARY,
                    )
                )
            )
            stale_edges: list[UUID5] = []
            if stale:
                stale_edges = list(
                    await opened.exec(
                        select(Fact.Claim.content_id)
                        .join(Fact.Content, Fact.Content.id == Fact.Claim.content_id)
                        .where(
                            Fact.Claim.scopes == scope_list,
                            Fact.Content.predicate == _PART_OF,
                            or_(
                                Fact.Content.subject_id.in_(stale),
                                Fact.Content.object_id.in_(stale),
                            ),
                        )
                    )
                )
                if stale_edges:
                    await opened.exec(
                        delete(Fact.Claim).where(
                            Fact.Claim.scopes == scope_list,
                            Fact.Claim.content_id.in_(stale_edges),
                        )
                    )
                await opened.exec(
                    delete(Entity.Claim).where(
                        Entity.Claim.scopes == scope_list,
                        Entity.Claim.content_id.in_(stale),
                    )
                )
            await Entity.Content.mint_all(opened, self.contents)
            opened.add_all(self.claims)
            await opened.flush()
            await Fact.Content.mint_all(opened, self.edges)
            opened.add_all(self.edge_claims)
            await opened.flush()
            if stale_edges:
                claimed = (
                    select(Fact.Claim.id).where(Fact.Claim.content_id == Fact.Content.id).exists()
                )
                await opened.exec(
                    delete(Fact.Content).where(
                        Fact.Content.id.in_(stale_edges),
                        ~claimed,
                    )
                )
            if stale:
                claimed_entity = (
                    select(Entity.Claim.id)
                    .where(Entity.Claim.content_id == Entity.Content.id)
                    .exists()
                )
                await opened.exec(
                    delete(Entity.Content).where(
                        Entity.Content.id.in_(stale),
                        ~claimed_entity,
                    )
                )


async def build_raptor(
    llm: LLM,
    embed: Embedder,
    scopes: Scopes | None = None,
) -> int:
    """Build and atomically replace one exact scope's recursive summary tree."""
    key = frozenset(scopes or (settings.system_user_id,))
    builder = RaptorBuilder(scopes=key, llm=llm, embed=embed)
    with span("raptor_snapshot"):
        async with User.system(key) as session:
            communities = list(
                await session.exec(
                    select(Community).where(
                        Community.embedding.is_not(None),
                        Community.scopes == sorted(key),
                    )
                )
            )
    with span("raptor_planning"):
        written = await builder.build(communities)
    with span("raptor_replacement"):
        await builder.replace()
    logger.info("raptor tree wrote {} summaries in scope {}", written, key)
    return written
