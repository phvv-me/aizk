from collections.abc import Callable
from datetime import datetime
from typing import cast

from loguru import logger
from patos import FlexModel, FrozenModel, sql
from pgvector.sqlalchemy import HALFVEC
from pydantic import UUID5, UUID7, Field, field_validator
from sqlalchemy import (
    Integer,
    Text,
    Uuid,
    column,
    func,
    literal,
    true,
)
from sqlalchemy.dialects.postgresql import Range, insert
from sqlalchemy.sql.selectable import CTE
from sqlmodel import select, tuple_
from sqlmodel.sql.expression import Select

from ..config import settings
from ..extract.models import ConsolidationVerdict, TimedFact
from ..ontology import Ontology
from ..provenance import CaptureContext
from ..store import Entity, Fact, Relation
from ..store.engine import Session
from ..store.models.tables import EntityContent, FactContent
from ..types import Scopes
from .consolidation import Consolidator, FactMatch
from .dedupe import ClaimField
from .grounding import quote_interval
from .ids import entity_id, fact_id
from .naming import normalize_name

# SQLModel synthesizes table-model keyword constructors outside the static signatures.
_entity_content = cast("Callable[..., EntityContent]", Entity.Content)
_fact_content = cast("Callable[..., FactContent]", Fact.Content)


class PreparedEntity(FrozenModel):
    """An extracted entity with its resolved type and precomputed name embedding."""

    name: str
    type: str
    vector: tuple[float, ...]


class FactCandidate(FrozenModel):
    """A new scoped fact whose referenced entities have already resolved."""

    fact: TimedFact
    subject_id: UUID5
    object_id: UUID5 | None
    identity: UUID5


class FactPlan(FrozenModel):
    """A fact candidate with its vector, ranked pool, and rule verdict."""

    candidate: FactCandidate
    vector: tuple[float, ...]
    matches: tuple[FactMatch, ...]
    verdict: ConsolidationVerdict | None


type _Write = tuple[int, FactPlan, ConsolidationVerdict]


class GraphWriter(FlexModel):
    """One graph-write round bound to the exact scope set every write in it shares."""

    session: Session
    created_by: UUID5
    scopes: list[UUID5]
    consolidator: Consolidator
    capture: CaptureContext = Field(default_factory=CaptureContext)
    source_text: str = ""

    @field_validator("scopes", mode="before")
    @classmethod
    def ordered_scopes(cls, scopes: Scopes) -> list[UUID5]:
        """Canonicalize the exact scope set into the sorted list every claim stores."""
        return sorted(scopes)

    def grounding(self, fact: TimedFact) -> dict[str, int]:
        """Char offsets of the fact's supporting quote inside the source chunk, when it
        aligns."""
        interval = quote_interval(fact.quote, self.source_text)
        if interval is None:
            return {}
        return {"quote_start": interval[0], "quote_end": interval[1]}

    async def resolve(self, entity: PreparedEntity) -> UUID5 | None:
        """Resolve one entity through the same set-based path used for a chunk."""
        return (await self.resolve_all([entity])).get(normalize_name(entity.name))

    async def resolve_all(self, entities: list[PreparedEntity]) -> dict[str, UUID5]:
        """Resolve a chunk by normalized identity and return normalized name to content ID.

        Semantic vector proximity is deliberately excluded from identity. Similar concepts
        such as Health, Business, and Research are related graph nodes, not aliases.
        """
        usable: list[tuple[PreparedEntity, UUID5]] = []
        for entity in entities:
            if normalize_name(entity.name):
                usable.append((entity, entity_id(entity.name, entity.type)))
            else:
                logger.warning("entity name {!r} is a path or link, dropping", entity.name)
        if not usable:
            return {}
        contents = {
            node: _entity_content(
                id=node,
                name=entity.name,
                type=entity.type,
                embedding=list(entity.vector),
            )
            for entity, node in usable
        }
        await Entity.Content.mint_all(
            self.session,
            list(contents.values()),
        )
        await Entity.Claim.claim_all(
            self.session,
            list(contents),
            self.created_by,
            frozenset(self.scopes),
        )
        return {normalize_name(entity.name): node for entity, node in usable}

    def candidate(self, fact: TimedFact, resolved: dict[str, UUID5]) -> FactCandidate | None:
        """Build a candidate when both named endpoints resolved to stored entities."""
        subject_id = resolved.get(normalize_name(fact.subject))
        if subject_id is None:
            logger.warning("fact subject {!r} has no resolved entity, skipping", fact.subject)
            return None
        object_id = resolved.get(normalize_name(fact.object_)) if fact.object_ else None
        if fact.object_ and object_id is None:
            logger.warning("fact object {!r} has no resolved entity, skipping", fact.object_)
            return None
        return FactCandidate(
            fact=fact,
            subject_id=subject_id,
            object_id=object_id,
            identity=fact_id(subject_id, fact.predicate, object_id, fact.statement),
        )

    async def new_candidates(
        self, facts: list[TimedFact], resolved: dict[str, UUID5]
    ) -> list[FactCandidate]:
        """The facts not already claimed by this container and whose subject resolved to a
        real entity, the consolidation cascade's first, free tier."""
        candidates = [candidate for fact in facts if (candidate := self.candidate(fact, resolved))]
        # Non-state candidates deduplicate on their exact identity, which already spans
        # subject, predicate, object, and statement, so distinct simultaneous facts all
        # survive. A state relation holds one current value per subject slot and window,
        # so same-slot state candidates inside one batch collapse to the first; letting
        # both through would revise the same current claim twice in one write.
        unique: dict[
            tuple[UUID5, str, datetime | None, datetime | None, str],
            FactCandidate,
        ] = {}
        for candidate in candidates:
            policy = Ontology.current().relation_policies[candidate.fact.predicate]
            state = policy == Relation.Policy.state
            unique.setdefault(
                (
                    candidate.subject_id if state else candidate.identity,
                    candidate.fact.predicate if state else "",
                    candidate.fact.valid_from if state else None,
                    candidate.fact.valid_to if state else None,
                    candidate.fact.kind.perspective_key(self.created_by),
                ),
                candidate,
            )
        candidates = list(unique.values())
        if not candidates:
            return []
        keys = [
            (candidate.identity, candidate.fact.kind.perspective_key(self.created_by))
            for candidate in candidates
        ]
        claimed = set(
            await self.session.exec(
                select(Fact.Claim.content_id, Fact.Claim.perspective_key).where(
                    Fact.Claim.scopes == self.scopes,
                    tuple_(Fact.Claim.content_id, Fact.Claim.perspective_key).in_(keys),
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
        matches = await self._fact_matches(self._fact_inputs(candidates, vectors), len(candidates))
        return [
            FactPlan(
                candidate=candidate,
                vector=tuple(vector),
                matches=tuple(ranked),
                verdict=self.consolidator.decide(
                    Ontology.current().relation_policies[candidate.fact.predicate],
                    candidate.object_id,
                    ranked,
                ),
            )
            for candidate, vector, ranked in zip(candidates, vectors, matches, strict=True)
        ]

    def _fact_inputs(self, candidates: list[FactCandidate], vectors: list[list[float]]) -> CTE:
        """Render fact candidates as one typed input relation."""
        return sql.relation(
            "fact_input",
            (
                column("ordinal", Integer),
                column("subject_id", Uuid),
                column("predicate", Text),
                column("perspective_key", Text),
                column("embedding", HALFVEC(settings.embed_dim)),
            ),
            [
                (
                    ordinal,
                    candidate.subject_id,
                    candidate.fact.predicate,
                    candidate.fact.kind.perspective_key(self.created_by),
                    vector,
                )
                for ordinal, (candidate, vector) in enumerate(
                    zip(candidates, vectors, strict=True)
                )
            ],
        )

    async def _fact_matches(self, inputs: CTE, count: int) -> list[list[FactMatch]]:
        """Return each input's nearest live facts from one lateral database query."""
        distance = Fact.Content.embedding @ inputs.c.embedding.cast(HALFVEC(settings.embed_dim))
        ranked = (
            select(
                Fact.Claim.id,
                Fact.Content.object_id,
                Fact.Content.statement,
                distance.label("distance"),
            )
            .select_from(Fact.Content.__table__.join(Fact.Claim.__table__))
            .where(
                Fact.Content.subject_id == inputs.c.subject_id,
                Fact.Content.predicate == inputs.c.predicate,
                Fact.Claim.scopes == self.scopes,
                Fact.Claim.perspective_key == inputs.c.perspective_key,
                Fact.Claim.is_current,
            )
            .order_by(distance)
            .limit(settings.similar_facts)
            .lateral("ranked_fact")
        )
        matches: list[list[FactMatch]] = [[] for _ in range(count)]
        rows = await self.session.exec(
            # `add_columns` erases the sqlmodel `Select` subtype statically while the
            # runtime object stays one, so the cast restores what `exec` requires.
            cast(
                "Select[tuple[int, UUID7 | None, UUID5 | None, str | None, float | None]]",
                select(
                    inputs.c.ordinal,
                    ranked.c.id,
                    ranked.c.object_id,
                    ranked.c.statement,
                )
                .add_columns(ranked.c.distance)
                .select_from(inputs.outerjoin(ranked, true()))
                .order_by(inputs.c.ordinal, ranked.c.distance),
            )
        )
        for ordinal, claim_id, object_id, statement, match_distance in rows:
            if claim_id is not None:
                matches[ordinal].append(
                    FactMatch(
                        id=claim_id,
                        object_id=object_id,
                        statement=cast(str, statement),
                        distance=cast(float, match_distance),
                    )
                )
        return matches

    def borderline(self, plans: list[FactPlan]) -> list[tuple[TimedFact, list[FactMatch]]]:
        """Return only plans whose similarity needs one batched LLM decision."""
        return [
            (plan.candidate.fact, list(plan.matches)) for plan in plans if plan.verdict is None
        ]

    async def resolve_ambiguous(self, plans: list[FactPlan]) -> list[ConsolidationVerdict]:
        """Resolve the plans left undecided by deterministic similarity rules."""
        return await self.consolidator.resolve(self.borderline(plans))

    async def lock_plans(self, plans: list[FactPlan]) -> None:
        """Serialize each scope, subject, and perspective slot for final revalidation."""
        slots = sorted(
            {
                (
                    plan.candidate.subject_id,
                    plan.candidate.fact.predicate,
                    plan.candidate.fact.kind.perspective_key(self.created_by),
                )
                for plan in plans
            },
            key=lambda slot: (str(slot[0]), slot[1], slot[2]),
        )
        if not slots:
            return
        inputs = sql.relation(
            "fact_lock",
            (
                column("subject_id", Uuid),
                column("predicate", Text),
                column("perspective_key", Text),
            ),
            slots,
        )
        key = func.hashtextextended(
            inputs.c.subject_id.cast(Text)
            + literal("|")
            + inputs.c.predicate
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
        source_chunk_id: UUID7,
    ) -> None:
        """Apply already-decided plans inside one short write transaction."""
        verdicts = merged_verdicts([plan.verdict for plan in plans], resolved)
        writes = self._writes(plans, verdicts)
        valid_ends = await Fact.Claim.revise(
            self.session,
            self._revisions(writes),
        )
        rows = self._write_rows(writes, valid_ends, source_chunk_id)
        contents = [content for content, _ in rows]
        claims = [claim for _, claim in rows]
        await Fact.Content.mint_all(self.session, contents)
        if claims:
            await self.session.exec(
                insert(Fact.Claim)
                .values(claims)
                .on_conflict_do_nothing(
                    index_elements=[
                        Fact.Claim.content_id,
                        Fact.Claim.scopes,
                        Fact.Claim.perspective_key,
                    ],
                    index_where=Fact.Claim.recorded.f.upper_inf(result=bool),
                )
            )

    @staticmethod
    def _writes(plans: list[FactPlan], verdicts: list[ConsolidationVerdict]) -> list[_Write]:
        """Pair every non-noop decision with its stable input ordinal."""
        return [
            (ordinal, plan, verdict)
            for ordinal, (plan, verdict) in enumerate(zip(plans, verdicts, strict=True))
            if verdict.action != "NOOP"
        ]

    @staticmethod
    def _revisions(
        writes: list[_Write],
    ) -> list[tuple[int, UUID7, datetime | None, datetime | None]]:
        """Close every occupied state slot and only the chosen claim for other updates."""
        revisions: list[tuple[int, UUID7, datetime | None, datetime | None]] = []
        for ordinal, plan, verdict in writes:
            if verdict.action != "UPDATE" or verdict.supersedes is None:
                continue
            policy = Ontology.current().relation_policies[plan.candidate.fact.predicate]
            claims = (
                [match.id for match in plan.matches]
                if policy == Relation.Policy.state
                else [verdict.supersedes]
            )
            revisions.extend(
                (
                    ordinal,
                    claim_id,
                    plan.candidate.fact.valid_from,
                    plan.candidate.fact.valid_to,
                )
                for claim_id in claims
            )
        return revisions

    def _write_rows(
        self,
        writes: list[_Write],
        valid_ends: dict[int, datetime | None],
        source_chunk_id: UUID7,
    ) -> list[tuple[FactContent, dict[str, ClaimField]]]:
        """Render decided writes using the temporal ends returned by PostgreSQL."""
        return [
            self._write_row(
                plan,
                source_chunk_id,
                valid_ends[ordinal]
                if verdict.action == "UPDATE" and verdict.supersedes is not None
                else plan.candidate.fact.valid_to,
            )
            for ordinal, plan, verdict in writes
        ]

    def _write_row(
        self,
        plan: FactPlan,
        source_chunk_id: UUID7,
        valid_to: datetime | None,
    ) -> tuple[FactContent, dict[str, ClaimField]]:
        """Render one decided fact as immutable content and its scoped temporal claim."""
        candidate = plan.candidate
        fact = candidate.fact
        return (
            _fact_content(
                id=candidate.identity,
                subject_id=candidate.subject_id,
                object_id=candidate.object_id,
                predicate=fact.predicate,
                statement=fact.statement,
                embedding=list(plan.vector),
            ),
            {
                "content_id": candidate.identity,
                "created_by": self.created_by,
                "scopes": self.scopes,
                "valid": Range(fact.valid_from, valid_to) if fact.valid_from or valid_to else None,
                "source_chunk_id": source_chunk_id,
                "attributes": self.capture.claim_attributes(fact.kind, self.created_by)
                | self.grounding(fact),
                "perspective_key": fact.kind.perspective_key(self.created_by),
            },
        )


def merged_verdicts(
    verdicts: list[ConsolidationVerdict | None], resolved: list[ConsolidationVerdict]
) -> list[ConsolidationVerdict]:
    """Fill each null, genuinely-ambiguous slot with the batched LLM's own verdict, in order."""
    pending = iter(resolved)
    return [verdict if verdict is not None else next(pending) for verdict in verdicts]
