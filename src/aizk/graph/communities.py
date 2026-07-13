import uuid

from loguru import logger
from networkx.algorithms.community.louvain import louvain_communities
from networkx.classes import Graph
from sqlalchemy import delete
from sqlmodel import select

from ..config import settings
from ..extract.llm import structured
from ..serving import embed
from ..store import Community, EntityContent, LiveFact
from ..store.identity import User
from ..types import Scopes
from .models import CommunitySummary


def detect(
    facts: list[LiveFact], min_size: int, backend: str = "networkx"
) -> list[set[uuid.UUID]]:
    """Detect entity communities over the latest-fact graph by Louvain modularity."""
    graph = Graph()
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
        entities: dict[uuid.UUID, EntityContent],
        facts: list[LiveFact],
    ) -> None:
        self.scopes = frozenset(scopes)
        self.entities = entities
        self.facts = facts

    def prompt(self, cluster: set[uuid.UUID]) -> str:
        """Render one cluster's entity roster and internal facts."""
        names = [self.entities[member].name for member in cluster if member in self.entities]
        statements = [
            fact.statement
            for fact in self.facts
            if fact.subject_id in cluster and (fact.object_id is None or fact.object_id in cluster)
        ]
        roster = "Entities: " + ", ".join(names)
        facts = "Facts:\n" + "\n".join(f"- {statement}" for statement in statements)
        return f"{roster}\n\n{facts}"

    async def rows(self, clusters: list[set[uuid.UUID]]) -> list[Community]:
        """Build all summary rows before the generation replacement begins."""
        reports = [
            await structured(
                settings.community_summary_system,
                self.prompt(cluster),
                CommunitySummary,
            )
            for cluster in clusters
        ]
        vectors = (
            await embed([report.summary for report in reports], mode="document") if reports else []
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
    async with User.system(key) as session:
        facts = list(await session.exec(LiveFact.embedded()))
        entity_ids = {
            entity_id
            for fact in facts
            for entity_id in (fact.subject_id, fact.object_id)
            if entity_id is not None
        }
        entities = {
            entity.id: entity
            for entity in await session.exec(
                select(EntityContent).where(EntityContent.id.in_(entity_ids))
            )
        }
    clusters = detect(facts, settings.community_min_size, settings.community_backend)
    rows = await CommunityBuilder(key, entities, facts).rows(clusters)
    async with User.system(key) as session:
        await session.exec(
            delete(Community)
            .where(Community.scopes == sorted(key))
            .execution_options(synchronize_session=False)
        )
        session.add_all(rows)
    logger.info("replaced {} communities in scope {}", len(rows), key)
    return len(rows)
