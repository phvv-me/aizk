import uuid
from datetime import UTC, datetime

from loguru import logger
from patos import FrozenModel
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import Integer, Text, Uuid, and_, cast, column, func, literal, true, values
from sqlalchemy import select as select_columns
from sqlalchemy.dialects.postgresql import Range, insert
from sqlmodel import select, tuple_

from ..config import settings
from ..extract.grounding import quote_interval
from ..extract.models import ConsolidationVerdict, TimedFact
from ..provenance import CaptureContext
from ..store import EntityClaim, EntityContent, FactClaim, FactContent
from ..store.engine import Session
from ..types import Scopes
from .consolidation import FactMatch, decide_by_rule
from .dedupe import ClaimField
from .ids import entity_id, fact_id
from .naming import normalize_name


class PreparedEntity(FrozenModel):
    """An extracted entity with its resolved type and precomputed name embedding."""

    name: str
    type: str
    vector: tuple[float, ...]


class FactCandidate(FrozenModel):
    """A new scoped fact whose referenced entities have already resolved."""

    fact: TimedFact
    subject_id: uuid.UUID
    object_id: uuid.UUID | None
    identity: uuid.UUID


class FactPlan(FrozenModel):
    """A fact candidate with its vector, ranked pool, and rule verdict."""

    candidate: FactCandidate
    vector: tuple[float, ...]
    matches: tuple[FactMatch, ...]
    verdict: ConsolidationVerdict | None


class GraphWriter:
    """One graph-write round bound to the exact scope set every write in it shares."""

    def __init__(
        self,
        session: Session,
        created_by: uuid.UUID,
        scopes: Scopes,
        capture: CaptureContext | None = None,
        source_text: str = "",
    ) -> None:
        self.session = session
        self.created_by = created_by
        self.scopes = sorted(scopes)
        self.capture = capture or CaptureContext()
        self.source_text = source_text

    def grounding(self, fact: TimedFact) -> dict[str, int]:
        """Char offsets of the fact's supporting quote inside the source chunk, when it
        aligns."""
        interval = quote_interval(fact.quote, self.source_text)
        if interval is None:
            return {}
        return {"quote_start": interval[0], "quote_end": interval[1]}

    async def resolve(self, entity: PreparedEntity) -> uuid.UUID | None:
        """Resolve one entity through the same set-based path used for a chunk."""
        return (await self.resolve_all([entity])).get(entity.name)

    async def resolve_all(self, entities: list[PreparedEntity]) -> dict[str, uuid.UUID]:
        """Resolve a chunk's entities with one claim read and bulk content and claim writes."""
        usable: list[tuple[PreparedEntity, uuid.UUID]] = []
        for entity in entities:
            if normalize_name(entity.name):
                usable.append((entity, entity_id(entity.name, entity.type)))
            else:
                logger.warning("entity name {!r} is a path or link, dropping", entity.name)
        if not usable:
            return {}
        inputs = (
            values(
                column("ordinal", Integer),
                column("id", Uuid),
                column("type", Text),
                column("embedding", HALFVEC(settings.embed_dim)),
                name="entity_input",
            )
            .data(
                [
                    (ordinal, node, entity.type, list(entity.vector))
                    for ordinal, (entity, node) in enumerate(usable)
                ]
            )
            .cte()
        )
        distance = EntityContent.embedding @ cast(inputs.c.embedding, HALFVEC(settings.embed_dim))
        nearest = (
            select(EntityContent.id)
            .where(
                EntityContent.type == inputs.c.type,
                distance <= 1.0 - settings.entity_resolution_threshold,
            )
            .order_by(distance)
            .limit(1)
            .lateral("nearest_entity")
        )
        rows = await self.session.exec(
            select(
                inputs.c.ordinal,
                func.coalesce(EntityClaim.content_id, nearest.c.id, inputs.c.id),
                and_(EntityClaim.content_id.is_(None), nearest.c.id.is_(None)),
            )
            .select_from(
                inputs.outerjoin(
                    EntityClaim.__table__,
                    and_(
                        EntityClaim.content_id == inputs.c.id,
                        EntityClaim.scopes == self.scopes,
                    ),
                ).outerjoin(nearest, EntityClaim.content_id.is_(None))
            )
            .order_by(inputs.c.ordinal)
        )
        resolved: dict[str, uuid.UUID] = {}
        new_contents: dict[uuid.UUID, PreparedEntity] = {}
        for ordinal, resolved_id, is_new in rows:
            entity, node = usable[ordinal]
            resolved[entity.name] = resolved_id
            if is_new:
                new_contents[node] = entity
        if new_contents:
            await EntityContent.mint_all(
                self.session,
                [
                    EntityContent(
                        id=node,
                        name=entity.name,
                        type=entity.type,
                        embedding=list(entity.vector),
                    )
                    for node, entity in new_contents.items()
                ],
            )
        claim_ids = set(resolved.values())
        await self.session.exec(
            insert(EntityClaim)
            .values(
                [
                    {
                        "content_id": content_id,
                        "created_by": self.created_by,
                        "scopes": self.scopes,
                    }
                    for content_id in claim_ids
                ]
            )
            .on_conflict_do_nothing(index_elements=[EntityClaim.content_id, EntityClaim.scopes])
        )
        return resolved

    def candidate(self, fact: TimedFact, resolved: dict[str, uuid.UUID]) -> FactCandidate | None:
        """Build a candidate when its subject resolved to a stored entity."""
        subject_id = resolved.get(fact.subject)
        if subject_id is None:
            logger.warning("fact subject {!r} has no resolved entity, skipping", fact.subject)
            return None
        object_id = resolved.get(fact.object_) if fact.object_ else None
        return FactCandidate(
            fact=fact,
            subject_id=subject_id,
            object_id=object_id,
            identity=fact_id(fact.subject, fact.predicate, fact.object_, fact.statement),
        )

    async def new_candidates(
        self, facts: list[TimedFact], resolved: dict[str, uuid.UUID]
    ) -> list[FactCandidate]:
        """The facts not already claimed by this container and whose subject resolved to a
        real entity, the consolidation cascade's first, free tier."""
        candidates = [candidate for fact in facts if (candidate := self.candidate(fact, resolved))]
        if not candidates:
            return []
        keys = [
            (candidate.identity, candidate.fact.kind.perspective_key(self.created_by))
            for candidate in candidates
        ]
        claimed = set(
            await self.session.exec(
                select(FactClaim.content_id, FactClaim.perspective_key).where(
                    FactClaim.scopes == self.scopes,
                    tuple_(FactClaim.content_id, FactClaim.perspective_key).in_(keys),
                )
            )
        )
        return [
            candidate
            for candidate in candidates
            if (
                candidate.identity,
                candidate.fact.kind.perspective_key(self.created_by),
            )
            not in claimed
        ]

    async def plan_facts(
        self,
        candidates: list[FactCandidate],
        vectors: list[list[float]],
    ) -> list[FactPlan]:
        """Rank the narrow top fact matches in PostgreSQL and apply deterministic verdicts."""
        if not candidates:
            return []
        inputs = (
            values(
                column("ordinal", Integer),
                column("subject_id", Uuid),
                column("perspective_key", Text),
                column("embedding", HALFVEC(settings.embed_dim)),
                name="fact_input",
            )
            .data(
                [
                    (
                        ordinal,
                        candidate.subject_id,
                        candidate.fact.kind.perspective_key(self.created_by),
                        vector,
                    )
                    for ordinal, (candidate, vector) in enumerate(
                        zip(candidates, vectors, strict=True)
                    )
                ]
            )
            .cte()
        )
        distance = FactContent.embedding @ cast(inputs.c.embedding, HALFVEC(settings.embed_dim))
        ranked = (
            select_columns(
                FactClaim.id,
                FactContent.predicate,
                FactContent.object_id,
                FactContent.statement,
                distance.label("distance"),
            )
            .select_from(FactContent.__table__.join(FactClaim.__table__))
            .where(
                FactContent.subject_id == inputs.c.subject_id,
                FactClaim.scopes == self.scopes,
                FactClaim.perspective_key == inputs.c.perspective_key,
                FactClaim.is_current,
            )
            .order_by(distance)
            .limit(settings.similar_facts)
            .lateral("ranked_fact")
        )
        matches: list[list[FactMatch]] = [[] for _ in candidates]
        rows = await self.session.exec(
            select_columns(
                inputs.c.ordinal,
                ranked.c.id,
                ranked.c.predicate,
                ranked.c.object_id,
                ranked.c.statement,
                ranked.c.distance,
            )
            .select_from(inputs.outerjoin(ranked, true()))
            .order_by(inputs.c.ordinal, ranked.c.distance)
        )
        for ordinal, claim_id, predicate, object_id, statement, match_distance in rows:
            if claim_id is not None:
                matches[ordinal].append(
                    FactMatch(
                        id=claim_id,
                        predicate=predicate,
                        object_id=object_id,
                        statement=statement,
                        distance=match_distance,
                    )
                )
        plans: list[FactPlan] = []
        for candidate, vector, ranked_matches in zip(candidates, vectors, matches, strict=True):
            fact = candidate.fact
            plans.append(
                FactPlan(
                    candidate=candidate,
                    vector=tuple(vector),
                    matches=tuple(ranked_matches),
                    verdict=decide_by_rule(fact.predicate, candidate.object_id, ranked_matches),
                )
            )
        return plans

    def borderline(self, plans: list[FactPlan]) -> list[tuple[TimedFact, list[FactMatch]]]:
        """Return only plans whose similarity needs one batched LLM decision."""
        return [
            (plan.candidate.fact, list(plan.matches)) for plan in plans if plan.verdict is None
        ]

    async def lock_plans(self, plans: list[FactPlan]) -> None:
        """Serialize each scope, subject, and perspective slot for final revalidation."""
        slots = sorted(
            {
                (
                    plan.candidate.subject_id,
                    plan.candidate.fact.kind.perspective_key(self.created_by),
                )
                for plan in plans
            },
            key=lambda slot: (str(slot[0]), slot[1]),
        )
        if not slots:
            return
        inputs = (
            values(
                column("subject_id", Uuid),
                column("perspective_key", Text),
                name="fact_lock",
            )
            .data(slots)
            .cte()
        )
        key = func.hashtextextended(
            cast(inputs.c.subject_id, Text)
            + literal("|")
            + inputs.c.perspective_key
            + literal("|")
            + literal(",".join(str(scope) for scope in self.scopes)),
            0,
        )
        await self.session.exec(
            select(func.pg_advisory_xact_lock(key)).select_from(inputs).order_by(key)
        )

    async def apply_plans(
        self,
        plans: list[FactPlan],
        resolved: list[ConsolidationVerdict],
        source_chunk_id: uuid.UUID,
    ) -> None:
        """Apply already-decided plans inside one short write transaction."""
        verdicts = merged_verdicts([plan.verdict for plan in plans], resolved)
        superseded = [
            verdict.supersedes
            for verdict in verdicts
            if verdict is not None
            and verdict.action == "UPDATE"
            and verdict.supersedes is not None
        ]
        retired = {
            claim.id: claim
            for claim in await self.session.exec(
                select(FactClaim)
                .where(FactClaim.id.in_(superseded))
                .execution_options(**{settings.skip_live_gate: True})
            )
        }
        contents: list[FactContent] = []
        claims: list[dict[str, ClaimField]] = []
        for plan, verdict in zip(plans, verdicts, strict=True):
            assert verdict is not None
            if verdict.action == "NOOP":
                continue
            candidate = plan.candidate
            fact = candidate.fact
            now = datetime.now(UTC)
            valid_to = fact.valid_to
            if verdict.action == "UPDATE" and verdict.supersedes is not None:
                previous = retired[verdict.supersedes]
                lower = previous.valid.lower if previous.valid else None
                if fact.valid_from is not None and lower is not None and fact.valid_from < lower:
                    if valid_to is None or lower < valid_to:
                        valid_to = lower
                else:
                    closing = fact.valid_from or now
                    if lower is not None and closing < lower:
                        closing = lower
                    previous.valid = Range(lower, closing)
                    previous.recorded = Range(previous.recorded.lower, now)
            contents.append(
                FactContent(
                    id=candidate.identity,
                    subject_id=candidate.subject_id,
                    object_id=candidate.object_id,
                    predicate=fact.predicate,
                    statement=fact.statement,
                    embedding=list(plan.vector),
                )
            )
            claims.append(
                {
                    "content_id": candidate.identity,
                    "created_by": self.created_by,
                    "scopes": self.scopes,
                    "valid": Range(fact.valid_from, valid_to)
                    if fact.valid_from or valid_to
                    else None,
                    "source_chunk_id": source_chunk_id,
                    "attributes": self.capture.claim_attributes(fact.kind, self.created_by)
                    | self.grounding(fact),
                    "perspective_key": fact.kind.perspective_key(self.created_by),
                }
            )
        await self.session.flush()
        await FactContent.mint_all(self.session, contents)
        if claims:
            await self.session.exec(
                insert(FactClaim)
                .values(claims)
                .on_conflict_do_nothing(
                    index_elements=[
                        FactClaim.content_id,
                        FactClaim.scopes,
                        FactClaim.perspective_key,
                    ],
                    index_where=func.upper_inf(FactClaim.recorded),
                )
            )


def merged_verdicts(
    verdicts: list[ConsolidationVerdict | None], resolved: list[ConsolidationVerdict]
) -> list[ConsolidationVerdict | None]:
    """Fill each null, genuinely-ambiguous slot with the batched LLM's own verdict, in order."""
    pending = iter(resolved)
    return [verdict if verdict is not None else next(pending) for verdict in verdicts]
