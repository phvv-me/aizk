import uuid

from loguru import logger
from sqlmodel import select

from ..config import settings
from ..extract import ontology
from ..extract.llm import structured
from ..serving import embed
from ..store import EntityContent, FactClaim, FactContent, LiveFact
from ..store.engine import Session
from ..store.identity import User
from ..types import Scopes
from .dedupe import claim_entity, claim_fact
from .ids import entity_id, fact_id
from .models import InsightReport, Observation

# the single node every observation hangs off, one per user, so the derived insights form one
# small structural subgraph the recall fact lane already surfaces rather than a scattered set.
OBSERVATION_NODE = "graph observations"


def kept_observations(report: InsightReport) -> list[Observation]:
    """The observations that clear the significance gate, capped at the per-run write limit."""
    significant = [
        obs for obs in report.observations if obs.significance >= settings.insight_min_significance
    ]
    significant.sort(key=lambda obs: obs.significance, reverse=True)
    return significant[: settings.insight_max]


async def observation_already_claimed(
    session: Session, scopes: Scopes, identity: uuid.UUID
) -> bool:
    """Whether this scope already stakes an observes claim on this content id, ever."""
    claimed = (
        await session.exec(
            select(FactClaim.id)
            .where(
                FactClaim.content_id == identity,
                FactClaim.scopes == sorted(scopes),
            )
            .execution_options(**{settings.skip_live_gate: True})
        )
    ).first()
    return claimed is not None


async def write_observation(
    session: Session,
    scopes: Scopes,
    node_id: uuid.UUID,
    obs: Observation,
    vector: list[float],
) -> bool:
    """Idempotently write one gated observation as an observes fact, returning whether it was
    new."""
    identity = fact_id(OBSERVATION_NODE, ontology.OBSERVES, "", obs.statement)
    if await observation_already_claimed(session, scopes, identity):
        return False
    await FactContent(
        id=identity,
        subject_id=node_id,
        object_id=None,
        predicate=ontology.OBSERVES,
        statement=obs.statement,
        embedding=vector,
    ).mint(session)
    await claim_fact(
        session,
        identity,
        settings.system_user_id,
        sorted(scopes),
        attributes={"significance": obs.significance},
    )
    return True


class InsightBuilder:
    """Derive and store significant observations in short database phases."""

    def __init__(self, scopes: Scopes) -> None:
        self.scopes = frozenset(scopes)

    async def grounding(self) -> list[str] | None:
        """The latest fact statements to reflect on, null when too few exist to ground on."""
        async with User.system(self.scopes) as session:
            statements = list(
                await session.exec(
                    LiveFact.newest_statements(settings.insight_facts_k).where(
                        LiveFact.predicate != ontology.OBSERVES
                    )
                )
            )
        if len(statements) < 2:
            logger.info("insight pass skipped for {}, too few facts to ground on", self.scopes)
            return None
        return statements

    async def store(self, kept: list[Observation], vectors: list[list[float]]) -> int:
        """Write gated observations as content-addressed, idempotent observes claims."""
        node_id = entity_id(OBSERVATION_NODE, ontology.OBSERVATION)
        async with User.system(self.scopes) as session:
            await EntityContent(
                id=node_id,
                name=OBSERVATION_NODE,
                type=ontology.OBSERVATION,
            ).mint(session)
            await claim_entity(session, node_id, settings.system_user_id, sorted(self.scopes))
            written = sum(
                [
                    await write_observation(session, self.scopes, node_id, obs, vector)
                    for obs, vector in zip(kept, vectors, strict=True)
                ]
            )
        logger.info("insight pass wrote {} observations for {}", written, self.scopes)
        return written

    async def build(self) -> int:
        """Run the snapshot, model, embedding, and write phases."""
        grounding = await self.grounding()
        if grounding is None:
            return 0
        report = await structured(
            settings.insight_system,
            "Facts:\n" + "\n".join(f"- {statement}" for statement in grounding),
            InsightReport,
        )
        kept = kept_observations(report)
        if not kept:
            logger.info(
                "insight pass wrote nothing for {}, no observation cleared the gate",
                self.scopes,
            )
            return 0
        vectors = await embed([observation.statement for observation in kept], mode="document")
        return await self.store(kept, vectors)


async def derive_insights(
    scopes: Scopes | None = None,
) -> int:
    """Derive observations from a user's graph and write the significant ones back."""
    return await InsightBuilder(frozenset(scopes or (settings.system_user_id,))).build()
