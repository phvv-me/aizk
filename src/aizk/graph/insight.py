import uuid

from loguru import logger
from sqlalchemy import func, select

from ..config import settings
from ..extract import ontology
from ..store import EntityContent, FactClaim, FactContent, LiveFact, acting_as
from ..store.engine import session
from .dedupe import claim_entity, claim_fact, mint_content
from .ids import entity_id, fact_id
from .models import InsightReport, Observation
from .tier_builder import TierBuilder

# the single node every observation hangs off, one per user, so the derived insights form one
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


async def observation_already_claimed(user_id: uuid.UUID, identity: uuid.UUID) -> bool:
    """Whether this user already stakes an observes claim on this exact content id, ever.

    user_id: identity the observation would be claimed under.
    identity: content-addressed id for the observation's statement.
    """
    claimed = await session().scalar(
        select(FactClaim.id)
        .where(
            FactClaim.content_id == identity,
            FactClaim.owner_id == user_id,
            FactClaim.scopes == [],
        )
        .execution_options(**{settings.skip_live_gate: True})
    )
    return claimed is not None


async def write_observation(
    user_id: uuid.UUID,
    node_id: uuid.UUID,
    obs: Observation,
    vector: list[float],
) -> bool:
    """Idempotently write one gated observation as an observes fact, returning whether it was new.

    user_id: identity the observation is claimed under, always privately (empty scopes).
    node_id: the OBSERVATION_NODE entity content id every observation hangs off.
    obs: the gated observation to write.
    vector: the observation's own statement, already embedded.
    """
    identity = fact_id(OBSERVATION_NODE, ontology.OBSERVES, "", obs.statement)
    if await observation_already_claimed(user_id, identity):
        return False
    await mint_content(
        FactContent(
            id=identity,
            subject_id=node_id,
            object_id=None,
            predicate=ontology.OBSERVES,
            statement=obs.statement,
            embedding=vector,
        ),
    )
    # an observation carries no scope of its own, always private to the user reflected on.
    await claim_fact(
        identity,
        user_id,
        [],
        attributes={"significance": obs.significance},
    )
    return True


class InsightTierBuilder(TierBuilder[list[str], InsightReport]):
    """The reflective pass over one user's whole graph, deriving higher-level observations.

    Grounds the reflection in the latest claims, asks the LLM for observations it scores itself,
    keeps only those that clear the significance gate, then writes each surviving one back as a
    content-addressed `observes` fact, content and claim both idempotent upserts, hanging off
    `OBSERVATION_NODE`, embedded so recall surfaces it beside the knowledge it rests on. A
    content-addressed id makes a rerun idempotent, so a stable insight is never claimed twice.
    """

    def __init__(self, user_id: uuid.UUID) -> None:
        super().__init__(user_id, settings.insight_system, InsightReport)

    async def gather(self) -> list[str] | None:
        """The latest fact statements to reflect on, null when too few exist to ground on."""
        async with acting_as(self.user_id):
            statements = list(
                await session().scalars(
                    select(LiveFact.statement)
                    .where(LiveFact.predicate != ontology.OBSERVES)
                    .order_by(func.lower(LiveFact.recorded).desc())
                    .limit(settings.insight_facts_k)
                )
            )
        if len(statements) < 2:
            logger.info("insight pass skipped for {}, too few facts to ground on", self.user_id)
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
                self.user_id,
            )
        return [obs.statement for obs in kept]

    async def upsert(
        self, grounding: list[str], report: InsightReport, vectors: list[list[float]]
    ) -> int:
        """Write each gated observation as its own content-addressed, idempotent observes claim."""
        kept = kept_observations(report)
        node_id = entity_id(OBSERVATION_NODE, ontology.OBSERVATION)
        async with acting_as(self.user_id):
            await mint_content(
                EntityContent(id=node_id, name=OBSERVATION_NODE, type=ontology.OBSERVATION),
            )
            await claim_entity(node_id, self.user_id, [])
            written = sum(
                [
                    await write_observation(self.user_id, node_id, obs, vector)
                    for obs, vector in zip(kept, vectors, strict=True)
                ]
            )
        logger.info("insight pass wrote {} observations for {}", written, self.user_id)
        return written


async def derive_insights(
    user_id: uuid.UUID | None = None,
) -> int:
    """Derive observations from a user's graph and write the significant ones back.

    user_id: identity whose graph is reflected on and that owns the written observations, the
        system user when null.
    """
    user_id = user_id or settings.system_user_id
    return await InsightTierBuilder(user_id).build()
