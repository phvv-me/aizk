import asyncio
from collections.abc import Sequence
from itertools import batched

from loguru import logger
from mainboard.profiling import span
from networkx.algorithms.community.louvain import louvain_communities
from networkx.classes import Graph
from patos import FrozenModel
from pydantic import UUID5
from sqlalchemy import delete
from sqlmodel import select

from ..config import settings
from ..serving.embed import EmbedClient
from ..serving.extract import LLM
from ..store import Community, Entity, Fact
from ..store.identity import User
from ..store.models.views import LiveFact
from ..types import Scopes
from .models import CommunitySummary


class CommunityFact(FrozenModel):
    """The narrow live-fact projection community detection and prompts consume."""

    subject_id: UUID5
    object_id: UUID5 | None
    statement: str


def detect(
    facts: Sequence[CommunityFact | LiveFact], min_size: int, backend: str = "networkx"
) -> list[set[UUID5]]:
    """Detect entity communities over the latest-fact graph by Louvain modularity."""
    graph: Graph[UUID5] = Graph()
    graph.add_edges_from(
        (fact.subject_id, fact.object_id) for fact in facts if fact.object_id is not None
    )
    if graph.number_of_edges() == 0:
        return []
    # the in-process default and a registered accelerator take different dispatch paths, so the
    # backend keyword is passed only when one is named and omitted entirely for plain networkx.
    if backend == "networkx":
        communities = louvain_communities(graph, seed=settings.louvain_seed)
    else:
        communities = louvain_communities(graph, seed=settings.louvain_seed, backend=backend)
    return [set(members) for members in communities if len(members) >= min_size]


class CommunityBuilder:
    """Summarize a graph snapshot into one complete community generation."""

    def __init__(
        self,
        scopes: Scopes,
        entities: dict[UUID5, str],
        facts: Sequence[CommunityFact | LiveFact],
    ) -> None:
        self.scopes = frozenset(scopes)
        self.entities = entities
        self.facts = facts

    def prompt(self, cluster: set[UUID5]) -> str:
        """Render one cluster's entity roster and internal facts."""
        names = sorted(self.entities[member] for member in cluster if member in self.entities)[
            : settings.community_entities_k
        ]
        statements = list(
            dict.fromkeys(
                fact.statement
                for fact in self.facts
                if fact.subject_id in cluster
                and (fact.object_id is None or fact.object_id in cluster)
            )
        )[: settings.community_facts_k]
        roster = "Entities: " + ", ".join(names)
        facts = "Facts:\n" + "\n".join(f"- {statement}" for statement in statements)
        return f"{roster}\n\n{facts}"

    async def rows(self, clusters: list[set[UUID5]]) -> list[Community]:
        """Build all summary rows before the generation replacement begins."""
        llm = LLM.from_settings(settings)
        reports: list[CommunitySummary] = []
        with span("community_summaries"):
            for group in batched(clusters, settings.community_build_concurrency, strict=False):
                reports.extend(
                    await asyncio.gather(
                        *(
                            llm.generate(
                                settings.community_summary_system,
                                self.prompt(cluster),
                                CommunitySummary,
                            )
                            for cluster in group
                        )
                    )
                )
        with span("community_embeddings"):
            vectors = (
                await EmbedClient.from_settings(settings).embed(
                    [report.summary for report in reports], mode="document"
                )
                if reports
                else []
            )
        return [
            Community(
                created_by=settings.system_user_id,
                scopes=sorted(self.scopes),
                label=report.label,
                summary=report.summary,
                embedding=vector,
                member_ids=list(cluster),
            )
            for cluster, report, vector in zip(clusters, reports, vectors, strict=True)
        ]


async def build_communities(
    scopes: Scopes | None = None,
) -> int:
    """Detect communities over the entity graph, summarize each, store the rows, return the
    count."""
    key = frozenset(scopes or (settings.system_user_id,))
    with span("community_snapshot"):
        async with User.system(key) as session:
            facts = [
                CommunityFact.model_validate(row, from_attributes=True)
                for row in await session.exec(
                    select(Fact.Live.subject_id, Fact.Live.object_id, Fact.Live.statement)
                    .where(Fact.Live.embedding.is_not(None))
                    .order_by(Fact.Live.id.desc())
                )
            ]
            entity_ids = {
                entity_id
                for fact in facts
                for entity_id in (fact.subject_id, fact.object_id)
                if entity_id is not None
            }
            entities = {
                entity_id: name
                for entity_id, name in await session.exec(
                    select(Entity.Content.id, Entity.Content.name).where(
                        Entity.Content.id.in_(entity_ids)
                    )
                )
            }
    with span("community_detection"):
        clusters = detect(facts, settings.community_min_size, settings.community_backend)
    rows = await CommunityBuilder(key, entities, facts).rows(clusters)
    with span("community_replacement"):
        async with User.system(key) as session:
            await session.exec(
                delete(Community)
                .where(Community.scopes == sorted(key))
                .execution_options(synchronize_session=False)
            )
            session.add_all(rows)
    logger.info("replaced {} communities in scope {}", len(rows), key)
    return len(rows)
