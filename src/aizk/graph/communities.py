import asyncio
from collections import defaultdict, deque
from collections.abc import Sequence
from itertools import batched
from typing import Self

from loguru import logger
from mainboard.profiling import span
from networkx.algorithms.community.louvain import louvain_communities
from networkx.classes import Graph
from patos import FrozenModel
from pydantic import UUID5
from sqlalchemy import delete
from sqlmodel import select

from ..config import Settings, settings
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


class CommunityDetector(FrozenModel):
    """Partition the latest-fact graph into themes small enough to summarize.

    Louvain maximizes modularity, and modularity carries a resolution limit, so it cannot see
    a community whose internal edges number fewer than roughly the square root of the whole
    graph's edges. One pass over a hub-shaped graph of fifty thousand entities therefore
    returns a few dozen clusters of thousands of members each, and a paragraph summarizing
    thousands of unrelated entities says nothing. So every oversized cluster is partitioned
    again on its own induced subgraph, where the edge count and the limit that follows it
    are both much smaller, until each leaf either fits `max_size` or refuses to split. The
    result stays one flat partition, since the levels above it are RAPTOR's job.
    """

    resolution: float
    max_size: int
    min_size: int
    max_depth: int
    seed: int
    backend: str

    @classmethod
    def from_settings(cls, config: Settings) -> Self:
        """Build the detector configured for this deployment's graph."""
        return cls(
            resolution=config.community_resolution,
            max_size=config.community_max_size,
            min_size=config.community_min_size,
            max_depth=config.community_max_depth,
            seed=config.louvain_seed,
            backend=config.community_backend,
        )

    def detect(self, facts: Sequence[CommunityFact | LiveFact]) -> list[set[UUID5]]:
        """Refine the entity graph until every kept theme is a readable size."""
        graph: Graph[UUID5] = Graph()
        graph.add_edges_from(
            (fact.subject_id, fact.object_id) for fact in facts if fact.object_id is not None
        )
        if graph.number_of_edges() == 0:
            return []
        leaves: list[set[UUID5]] = []
        pending = deque((cluster, 0) for cluster in self.partition(graph))
        while pending:
            cluster, depth = pending.popleft()
            # A first-level cluster holding every node is the pass that just ran, so asking
            # for it again would repeat the most expensive call in the build to learn nothing.
            repeats = depth == 0 and len(cluster) == graph.number_of_nodes()
            refined = (
                self.partition(graph.subgraph(cluster))
                if len(cluster) > self.max_size and depth < self.max_depth and not repeats
                else []
            )
            # A cluster Louvain returns whole, a star around one hub for instance, has no
            # split left to find and stays as it is rather than looping forever.
            if len(refined) > 1:
                pending.extend((part, depth + 1) for part in refined)
            else:
                leaves.append(cluster)
        return [cluster for cluster in leaves if len(cluster) >= self.min_size]

    def partition(self, graph: Graph[UUID5]) -> list[set[UUID5]]:
        """Run one Louvain pass, naming the backend only when an accelerator is configured."""
        # the in-process default and a registered accelerator take different dispatch paths, so
        # the backend keyword is passed only when one is named and omitted for plain networkx.
        accelerator = {} if self.backend == "networkx" else {"backend": self.backend}
        return [
            set(members)
            for members in louvain_communities(
                graph, resolution=self.resolution, seed=self.seed, **accelerator
            )
        ]


class ClusterSummary(FrozenModel):
    """One cluster's label and summary, and whether a model actually wrote it."""

    label: str
    summary: str
    degraded: bool = False


class CommunityBuilder:
    """Summarize a graph snapshot into one complete community generation."""

    def __init__(
        self,
        scopes: Scopes,
        entities: dict[UUID5, str],
        facts: Sequence[CommunityFact | LiveFact],
        stored: Sequence[Community] = (),
    ) -> None:
        self.scopes = frozenset(scopes)
        self.entities = entities
        # The stored generation indexed by exactly the membership that produced it. A theme
        # whose members did not move is the same theme, so it is carried forward instead of
        # paying for another model call, which is what keeps a weekly rebuild proportional to
        # what changed rather than to the size of the graph.
        self.stored = {
            frozenset(row.member_ids): row for row in stored if row.embedding is not None
        }
        # Facts indexed by subject, carrying their position so a prompt still reads newest
        # first. Rescanning every fact for every cluster was affordable while a graph made a
        # handful of them and is not now that a refined partition makes thousands.
        self.by_subject: dict[UUID5, list[tuple[int, CommunityFact | LiveFact]]] = defaultdict(
            list
        )
        for position, fact in enumerate(facts):
            self.by_subject[fact.subject_id].append((position, fact))

    def prompt(self, cluster: set[UUID5]) -> str:
        """Render one cluster's entity roster and internal facts."""
        names = sorted(self.entities[member] for member in cluster if member in self.entities)[
            : settings.community_entities_k
        ]
        internal = sorted(
            (
                (position, fact)
                for member in cluster
                for position, fact in self.by_subject.get(member, ())
                if fact.object_id is None or fact.object_id in cluster
            ),
            key=lambda held: held[0],
        )
        statements = list(dict.fromkeys(fact.statement for _, fact in internal))[
            : settings.community_facts_k
        ]
        roster = "Entities: " + ", ".join(names)
        facts = "Facts:\n" + "\n".join(f"- {statement}" for statement in statements)
        return f"{roster}\n\n{facts}"

    async def rows(self, clusters: list[set[UUID5]]) -> list[Community]:
        """Build all summary rows before the generation replacement begins.

        A rebuild of a large graph is thousands of model calls, so one refused call must not
        throw away the thousands that answered. Every unchanged theme is carried forward from
        the stored generation, every changed one is summarized, and a failed call degrades to
        the cluster's own roster. Only a run that degraded more of its themes than
        `community_summary_failure_ratio` allows abandons the generation, which leaves the
        previous one standing rather than replacing it with rosters.
        """
        held = [self.stored.get(frozenset(cluster)) for cluster in clusters]
        changed = [cluster for cluster, row in zip(clusters, held, strict=True) if row is None]
        fresh = iter(await self.summaries(changed))
        summaries = [
            next(fresh) if row is None else ClusterSummary(label=row.label, summary=row.summary)
            for row in held
        ]
        degraded = sum(summary.degraded for summary in summaries)
        logger.info(
            "summarized {} themes, carried {} forward, degraded {}",
            len(changed) - degraded,
            len(clusters) - len(changed),
            degraded,
        )
        if degraded > len(clusters) * settings.community_summary_failure_ratio:
            raise RuntimeError(
                f"{degraded} of {len(clusters)} community summaries failed,"
                " keeping the stored generation"
            )
        with span("community_embeddings"):
            written = [
                summary.summary
                for summary, row in zip(summaries, held, strict=True)
                if row is None
            ]
            vectors = iter(
                await EmbedClient.from_settings(settings).embed(written, mode="document")
                if written
                else []
            )
        return [
            Community(
                created_by=settings.system_user_id,
                scopes=sorted(self.scopes),
                label=summary.label,
                summary=summary.summary,
                embedding=row.embedding if row is not None else next(vectors),
                member_ids=list(cluster),
            )
            for cluster, summary, row in zip(clusters, summaries, held, strict=True)
        ]

    async def summaries(self, clusters: list[set[UUID5]]) -> list[ClusterSummary]:
        """Ask the model for one summary per cluster, degrading a failed call to a roster."""
        llm = LLM.from_settings(settings)
        written: list[ClusterSummary] = []
        with span("community_summaries"):
            for group in batched(clusters, settings.community_build_concurrency, strict=False):
                reports = await asyncio.gather(
                    *(
                        llm.generate(
                            settings.community_summary_system,
                            self.prompt(cluster),
                            CommunitySummary,
                        )
                        for cluster in group
                    ),
                    return_exceptions=True,
                )
                for cluster, report in zip(group, reports, strict=True):
                    if isinstance(report, BaseException):
                        logger.warning("community summary failed, writing a roster: {}", report)
                        written.append(self.roster(cluster))
                    else:
                        written.append(ClusterSummary(label=report.label, summary=report.summary))
        return written

    def roster(self, cluster: set[UUID5]) -> ClusterSummary:
        """Name a cluster from its own members, the stand-in for a summary nobody wrote."""
        names = sorted(self.entities[member] for member in cluster if member in self.entities)
        head = names[0] if names else "Unnamed subjects"
        listed = ", ".join(names[: settings.community_entities_k])
        return ClusterSummary(
            label=f"{head} and {len(cluster) - 1} related subjects",
            summary=f"Unsummarized theme covering {listed}.",
            degraded=True,
        )


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
                for entity_id, name in await session.exec(Entity.Content.names_of(entity_ids))
            }
            stored = list(
                await session.exec(select(Community).where(Community.scopes == sorted(key)))
            )
    # Detection is minutes of pure Python on a large graph, and the worker that called it
    # still has a queue to serve, so it runs off the event loop.
    with span("community_detection"):
        clusters = await asyncio.to_thread(CommunityDetector.from_settings(settings).detect, facts)
    logger.info(
        "detected {} communities over {} facts, largest {} members, {} of {} entities in no theme",
        len(clusters),
        len(facts),
        max((len(cluster) for cluster in clusters), default=0),
        len(entity_ids) - sum(len(cluster) for cluster in clusters),
        len(entity_ids),
    )
    rows = await CommunityBuilder(key, entities, facts, stored).rows(clusters)
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
