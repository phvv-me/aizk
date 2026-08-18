from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import ClassVar, cast

from patos import sql
from pydantic import UUID5, UUID7, JsonValue
from sqlalchemy import Column as SAColumn
from sqlalchemy import (
    ColumnElement,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Table,
    Text,
    Uuid,
    and_,
    case,
    column,
    extract,
    func,
    literal,
    or_,
    update,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import declared_attr
from sqlmodel import select

from ....config import settings
from ....provenance import Stance
from ....types import Scopes
from ...engine import Session
from ...mixins import ClaimedContent, DeterministicId, Embedded, Id, Scoped, TableBase
from .chunk import Chunk
from .entity import EntityContent
from .ontology import RelationKind


class FactClaim(Id, Scoped, TableBase, table=True):
    """A scope set's bi-temporal and behavioral claim on shared fact content."""

    mutable: ClassVar[bool] = True

    content_id = sql.Field(
        UUID5,
        foreign_key="fact_content.id",
        ondelete="CASCADE",
        index=True,
    )
    valid_from = sql.Field(
        datetime | None,
        default=None,
        sa_type=DateTime(timezone=True),
    )
    valid_to = sql.Field(
        datetime | None,
        default=None,
        sa_type=DateTime(timezone=True),
    )
    recorded_from = sql.Field(
        datetime,
        default=None,
        sa_type=DateTime(timezone=True),
        server_default=func.now(),
    )
    recorded_to = sql.Field(
        datetime | None,
        default=None,
        sa_type=DateTime(timezone=True),
    )
    last_accessed = sql.Nullable(datetime)
    access_count = sql.Field(int, default=0)
    attributes = sql.Field(
        dict,
        default_factory=dict,
        sa_type=sql.TypedJSONB,
    )
    perspective_key = sql.Field(
        str,
        default="world",
        index=True,
        sa_type=String,
    )
    source_chunk_id = sql.FK(
        Chunk.id,
        nullable=True,
        ondelete="SET NULL",
        index=True,
    )
    promoted_from = sql.Field(
        UUID7 | None,
        foreign_key="fact_claim.id",
        ondelete="SET NULL",
        index=True,
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index, ...]:
        return (
            Index("ix_fact_claim_valid", "valid_from", "valid_to"),
            Index("ix_fact_claim_recorded", "recorded_from", "recorded_to"),
            Index(
                "ix_fact_claim_live",
                "valid_from",
                "valid_to",
                postgresql_where=SAColumn("recorded_to").is_(None),
            ),
            Index(
                "uq_fact_claim_live",
                "content_id",
                "scopes",
                "perspective_key",
                unique=True,
                postgresql_where=SAColumn("recorded_to").is_(None),
            ),
            Index("ix_fact_claim_scopes", "scopes", postgresql_using="gin"),
        )

    @classmethod
    def _is_current_predicate(cls) -> ColumnElement[bool]:
        return and_(
            cls.recorded_to.is_(None),
            or_(cls.valid_from.is_(None), cls.valid_from <= func.now()),
            or_(cls.valid_to.is_(None), cls.valid_to > func.now()),
        )

    @hybrid_property
    def is_current(self) -> bool:
        """Whether this claim is open in recorded and valid time."""
        now = datetime.now(UTC)
        return bool(
            self.recorded_to is None
            and (self.valid_from is None or self.valid_from <= now)
            and (self.valid_to is None or self.valid_to > now)
        )

    @is_current.inplace.expression
    @classmethod
    def is_current_expression(cls) -> ColumnElement[bool]:
        return cls._is_current_predicate()

    @hybrid_property
    def created_at(self) -> datetime:
        """Return the claim's first recorded time."""
        return self.recorded_from

    @created_at.inplace.expression
    @classmethod
    def created_at_expression(cls) -> ColumnElement[datetime]:
        return cls.recorded_from

    @classmethod
    def visible_at(cls, as_of: datetime | None) -> tuple[ColumnElement[bool], ...]:
        """Build current or historical bi-temporal visibility predicates."""
        if as_of is None:
            return (cls._is_current_predicate(),)
        return (
            or_(cls.valid_from.is_(None), cls.valid_from <= as_of),
            or_(cls.valid_to.is_(None), cls.valid_to > as_of),
            cls.recorded_from <= as_of,
            or_(cls.recorded_to.is_(None), cls.recorded_to > as_of),
        )

    def relevance(self, now: datetime, half_life_days: float) -> float:
        """Score access recency and frequency with exponential decay."""
        reference = self.last_accessed or self.recorded_from
        age_days = (now - reference) / timedelta(days=1)
        return cast(float, 0.5 ** (age_days / half_life_days) * (1 + self.access_count))

    @classmethod
    async def record_access(cls, session: Session, claim_ids: list[UUID7]) -> None:
        """Update recency and frequency for surfaced live facts in one statement."""
        if not claim_ids:
            return
        await session.exec(
            update(cls)
            .where(
                cls.recorded_to.is_(None),
                cls.id.in_(claim_ids),
            )
            .values(last_accessed=func.now(), access_count=cls.access_count + 1)
            .execution_options(synchronize_session=False)
        )

    @classmethod
    async def revise(
        cls,
        session: Session,
        revisions: Sequence[tuple[int, UUID7, datetime | None, datetime | None]],
    ) -> dict[int, datetime | None]:
        """Apply temporal corrections together and return each new claim's adjusted end."""
        if not revisions:
            return {}
        inputs = sql.relation(
            "fact_revision",
            (
                column("ordinal", Integer),
                column("id", Uuid),
                column("valid_from", DateTime(timezone=True)),
                column("valid_to", DateTime(timezone=True)),
            ),
            list(revisions),
        )
        valid_from = inputs.c.valid_from.cast(DateTime(timezone=True))
        input_valid_to = inputs.c.valid_to.cast(DateTime(timezone=True))
        lower = cls.valid_from
        backdated = and_(
            valid_from.is_not(None),
            lower.is_not(None),
            valid_from < lower,
        )
        closing = func.greatest(func.coalesce(valid_from, func.now()), lower)
        adjusted_valid_to = case(
            (backdated & input_valid_to.is_(None), lower),
            (backdated, func.least(input_valid_to, lower)),
            else_=input_valid_to,
        )
        rows = await session.exec(
            update(cls)
            .where(cls.id == inputs.c.id)
            .values(
                valid_to=case((backdated, cls.valid_to), else_=closing),
                recorded_to=case((backdated, cls.recorded_to), else_=func.now()),
            )
            .returning(inputs.c.ordinal, adjusted_valid_to)
            .execution_options(**{settings.skip_live_gate: True})
        )
        return dict(cast("Sequence[tuple[int, datetime | None]]", rows.all()))

    @classmethod
    async def archive_stale(
        cls,
        session: Session,
        scopes: Scopes,
        half_life_days: float,
        floor: float,
    ) -> list[UUID7]:
        """Close live claims below the relevance floor and return their IDs.

        The clock, the range close, and the decay stamp all come from the database's own
        now(), so one statement decides staleness with no host-side time conversion.
        """
        half_lives = (
            extract(
                "epoch",
                func.now() - cls.last_accessed.coalesce(cls.recorded_from),
            )
            / timedelta(days=1).total_seconds()
            / half_life_days
        )
        relevance = func.power(literal(0.5, Float), half_lives) * (
            literal(1.0, Float) + cls.access_count.cast(Float)
        )
        result = await session.exec(
            update(cls)
            .where(
                cls.recorded_to.is_(None),
                or_(cls.valid_from.is_(None), cls.valid_from <= func.now()),
                or_(cls.valid_to.is_(None), cls.valid_to > func.now()),
                cls.scopes == sorted(scopes),
                relevance < floor,
            )
            .values(
                recorded_to=func.now(),
                attributes=cls.attributes.op("||")(func.jsonb_build_object("decayed", func.now())),
            )
            .returning(cls.id)
            .execution_options(synchronize_session=False)
        )
        return [row[0] for row in result]

    @staticmethod
    def _stamp(marks: Mapping[str, str]) -> ColumnElement[dict[str, JsonValue]]:
        """One JSONB object of text marks, ready to merge into a claim's attributes."""
        return cast(
            "ColumnElement[dict[str, JsonValue]]",
            func.jsonb_build_object(
                *(part for key, value in marks.items() for part in (key, literal(value, Text)))
            ),
        )

    @classmethod
    async def _close(
        cls, session: Session, selected: ColumnElement[bool], marks: Mapping[str, str]
    ) -> list[UUID7]:
        """Close every live claim `selected` names now, stamping why history stops there."""
        result = await session.exec(
            update(cls)
            .where(cls.recorded_to.is_(None), selected)
            .values(
                recorded_to=literal(datetime.now(UTC), DateTime(timezone=True)),
                attributes=cls.attributes.op("||")(cls._stamp(marks)),
            )
            .returning(cls.id)
            .execution_options(synchronize_session=False)
        )
        return [row[0] for row in result]

    @classmethod
    async def retract_from_documents(
        cls,
        session: Session,
        document_ids: list[UUID7],
        reason: str,
    ) -> list[UUID7]:
        """Close live claims derived from documents before their chunks change."""
        return await cls._close(
            session,
            cls.source_chunk_id.in_(select(Chunk.id).where(Chunk.document_id.in_(document_ids))),
            {reason: datetime.now(UTC).isoformat()},
        )

    @classmethod
    async def forget_from_documents(
        cls, session: Session, document_ids: list[UUID7]
    ) -> list[UUID7]:
        """Retract live claims derived from explicitly forgotten documents."""
        return await cls.retract_from_documents(session, document_ids, "forgotten")

    @classmethod
    async def refute(
        cls, session: Session, claim_ids: Sequence[UUID7], evidence: UUID7
    ) -> list[UUID7]:
        """Close live claims a newly recorded fact disproves, naming what disproved them.

        This is the cross-document retraction. A source replaced in place already closes
        everything built from its old text, but a later, different document that shows an
        earlier claim was wrong has nothing to close it, so the claim stays live and keeps
        answering questions. `evidence` is the chunk the refutation was read from, kept on
        the closed claim so the retraction can be audited rather than merely trusted.
        """
        if not claim_ids:
            return []
        return await cls._close(
            session,
            cls.id.in_(claim_ids),
            {
                "stance": Stance.refuted.value,
                "refuted_at": datetime.now(UTC).isoformat(),
                "refuted_by_chunk": str(evidence),
            },
        )

    @classmethod
    async def dispute(cls, session: Session, claim_ids: Sequence[UUID7], evidence: UUID7) -> None:
        """Mark live claims a new fact contests, so find can never render them settled.

        The claims stay live and keep answering, because a source that only doubts an
        earlier one is no grounds to withdraw it. What changes is that both sides now
        render as disputed, which is the honest reading of a graph holding two.
        """
        if not claim_ids:
            return
        await session.exec(
            update(cls)
            .where(cls.recorded_to.is_(None), cls.id.in_(claim_ids))
            .values(
                attributes=cls.attributes.op("||")(
                    cls._stamp(
                        {
                            "stance": Stance.disputed.value,
                            "disputed_at": datetime.now(UTC).isoformat(),
                            "disputed_by_chunk": str(evidence),
                        }
                    )
                )
            )
            .execution_options(synchronize_session=False)
        )


class FactContent(DeterministicId, Embedded, ClaimedContent, TableBase, table=True):
    """Immutable, content-addressed graph edge shared by visible claims."""

    subject_id = sql.FK(
        EntityContent.id,
        ondelete="CASCADE",
        index=True,
    )
    object_id = sql.FK(
        EntityContent.id,
        nullable=True,
        ondelete="CASCADE",
        index=True,
    )
    predicate = sql.FK(RelationKind.name)
    statement = sql.Field(str)
    claim_table: ClassVar[Table] = FactClaim.__table__
