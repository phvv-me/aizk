import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from ..config import settings
from ..extract.ontology import EntityType, RelationType
from ..store import EntityClaim, EntityContent, FactClaim, FactContent, LiveFact, acting_as
from .dedupe import mint_content
from .ids import entity_id, fact_id
from .models import InsightReport, Observation
from .tier_builder import TierBuilder

# the single node every observation hangs off, one per principal, so the derived insights form one
# small structural subgraph the recall fact lane already surfaces rather than a scattered set.
OBSERVATION_NODE = "graph observations"


def kept_observations(report: InsightReport) -> list[Observation]:
    """The observations that clear the significance gate, capped at the per-run write limit.

    The selective-write gate, keeping only the observations whose significance reaches the floor
    so low-value self-talk never reaches the graph, then the highest-scoring few up to insight_max
    so a noisy model cannot flood one pass with derived facts.

    report: the reflective pass's candidate observations.
    """
    significant = [
        obs for obs in report.observations if obs.significance >= settings.insight_min_significance
    ]
    significant.sort(key=lambda obs: obs.significance, reverse=True)
    return significant[: settings.insight_max]


class InsightTierBuilder(TierBuilder[list[str], InsightReport]):
    """The reflective pass over one principal's whole graph, deriving higher-level observations.

    Grounds the reflection in the latest claims, asks the LLM for observations it scores itself,
    keeps only those that clear the significance gate, then writes each surviving one back as a
    content-addressed `observes` fact, content and claim both idempotent upserts, hanging off
    `OBSERVATION_NODE`, embedded so recall surfaces it beside the knowledge it rests on. A
    content-addressed id makes a rerun idempotent, so a stable insight is never claimed twice.
    """

    def __init__(self, principal_id: uuid.UUID) -> None:
        super().__init__(principal_id, settings.insight_system, InsightReport)

    async def gather(self) -> list[str] | None:
        """The latest fact statements to reflect on, null when too few exist to ground on."""
        async with acting_as(self.principal_id) as session:
            statements = list(
                await session.scalars(
                    select(LiveFact.statement)
                    .where(LiveFact.predicate != RelationType.OBSERVES)
                    .order_by(func.lower(LiveFact.recorded).desc())
                    .limit(settings.insight_facts_k)
                )
            )
        if len(statements) < 2:
            logger.info(
                "insight pass skipped for {}, too few facts to ground on", self.principal_id
            )
            return None
        return statements

    def body(self, grounding: list[str]) -> str:
        """Render the grounding fact statements as the structured call's user turn."""
        return "Facts:\n" + "\n".join(f"- {statement}" for statement in grounding)

    def texts(self, report: InsightReport) -> list[str]:
        """The statements of the observations that clear the significance gate."""
        kept = kept_observations(report)
        if not kept:
            logger.info(
                "insight pass wrote nothing for {}, no observation cleared the gate",
                self.principal_id,
            )
        return [obs.statement for obs in kept]

    async def upsert(
        self, grounding: list[str], report: InsightReport, vectors: list[list[float]]
    ) -> int:
        """Write each gated observation as its own content-addressed, idempotent observes claim."""
        kept = kept_observations(report)
        node_id = entity_id(OBSERVATION_NODE, EntityType.OBSERVATION)
        written = 0
        async with acting_as(self.principal_id) as session:
            await mint_content(
                session,
                EntityContent(id=node_id, name=OBSERVATION_NODE, type=EntityType.OBSERVATION),
            )
            await session.execute(
                insert(EntityClaim)
                .values(content_id=node_id, owner_id=self.principal_id, scope=None)
                .on_conflict_do_nothing(index_elements=["content_id", "owner_id", "scope"])
            )
            for obs, vector in zip(kept, vectors, strict=True):
                identity = fact_id(OBSERVATION_NODE, RelationType.OBSERVES, "", obs.statement)
                claimed = await session.scalar(
                    select(FactClaim.id)
                    .where(
                        FactClaim.content_id == identity,
                        FactClaim.owner_id == self.principal_id,
                        FactClaim.scope.is_(None),
                    )
                    .execution_options(**{settings.skip_live_gate: True})
                )
                if claimed is not None:
                    continue
                await mint_content(
                    session,
                    FactContent(
                        id=identity,
                        subject_id=node_id,
                        object_id=None,
                        predicate=RelationType.OBSERVES,
                        statement=obs.statement,
                        embedding=vector,
                    ),
                )
                await session.execute(
                    insert(FactClaim)
                    .values(
                        content_id=identity,
                        owner_id=self.principal_id,
                        scope=None,
                        attributes={"significance": obs.significance},
                        # an observation carries no scope of its own, always private to the
                        # principal reflected on, so it stamps reviewed immediately like any
                        # other private write
                        reviewed_at=datetime.now(UTC),
                    )
                    .on_conflict_do_nothing(
                        index_elements=["content_id", "owner_id", "scope"],
                        index_where=text("upper_inf(recorded)"),
                    )
                )
                written += 1
        logger.info("insight pass wrote {} observations for {}", written, self.principal_id)
        return written


async def derive_insights(
    principal_id: uuid.UUID | None = None,
) -> int:
    """Derive observations from a principal's graph and write the significant ones back.

    principal_id: identity whose graph is reflected on and that owns the written observations, the
        system principal when null.
    """
    principal_id = principal_id or settings.system_principal_id
    return await InsightTierBuilder(principal_id).build()
