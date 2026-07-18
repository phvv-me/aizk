from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from patos import sql
from pydantic import UUID5, UUID7
from sqlalchemy import (
    Boolean,
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
from sqlalchemy import Column as SAColumn
from sqlalchemy.dialects.postgresql import TSTZRANGE, Range
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import declared_attr
from sqlmodel import select

from ....config import settings
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
    valid = sql.Field(Range[datetime] | None, sa_type=TSTZRANGE)
    recorded = sql.Field(
        Range[datetime],
        default=None,
        sa_type=TSTZRANGE,
        server_default=func.tstzrange(func.now(), None, "[)"),
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
            Index("ix_fact_claim_valid", "valid", postgresql_using="gist"),
            Index("ix_fact_claim_recorded", "recorded", postgresql_using="gist"),
            Index(
                "ix_fact_claim_live",
                "valid",
                postgresql_using="gist",
                postgresql_where=func.upper_inf(SAColumn("recorded"), type_=Boolean),
            ),
            Index(
                "uq_fact_claim_live",
                "content_id",
                "scopes",
                "perspective_key",
                unique=True,
                postgresql_where=func.upper_inf(SAColumn("recorded"), type_=Boolean),
            ),
            Index("ix_fact_claim_scopes", "scopes", postgresql_using="gin"),
        )

    @classmethod
    def _is_current_predicate(cls) -> ColumnElement[bool]:
        return and_(
            cls.recorded.f.upper_inf(result=bool),
            or_(cls.valid.is_(None), cls.valid.contains(func.now())),
        )

    @hybrid_property
    def is_current(self) -> bool:
        """Whether this claim is open in recorded and valid time."""
        now = datetime.now(UTC)
        return bool(self.recorded.upper_inf and (self.valid is None or now in self.valid))

    @is_current.inplace.expression
    @classmethod
    def is_current_expression(cls) -> ColumnElement[bool]:
        return cls._is_current_predicate()

    @hybrid_property
    def created_at(self) -> datetime:
        """Return the claim's first recorded time."""
        assert self.recorded.lower is not None
        return self.recorded.lower

    @created_at.inplace.expression
    @classmethod
    def created_at_expression(cls) -> ColumnElement[datetime]:
        return cls.recorded.lower(result=datetime)

    @classmethod
    def visible_at(cls, as_of: datetime | None) -> tuple[ColumnElement[bool], ...]:
        """Build current or historical bi-temporal visibility predicates."""
        if as_of is None:
            return (cls._is_current_predicate(),)
        return (
            or_(cls.valid.is_(None), cls.valid.contains(as_of)),
            cls.recorded.contains(as_of),
        )

    def relevance(self, now: datetime, half_life_days: float) -> float:
        """Score access recency and frequency with exponential decay."""
        reference = self.last_accessed or self.recorded.lower
        assert reference is not None
        age_days = (now - reference) / timedelta(days=1)
        return 0.5 ** (age_days / half_life_days) * (1 + self.access_count)

    @classmethod
    async def record_access(cls, session: Session, claim_ids: list[UUID7]) -> None:
        """Update recency and frequency for surfaced live facts in one statement."""
        if not claim_ids:
            return
        await session.exec(
            update(cls)
            .where(
                cls.recorded.f.upper_inf(result=bool),
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
        lower = cls.valid.lower(result=datetime)
        backdated = and_(
            valid_from.is_not(None),
            lower.is_not(None),
            valid_from < lower,
        )
        closing = func.greatest(func.coalesce(valid_from, func.now()), lower)
        valid_to = case(
            (backdated, func.least(input_valid_to, lower)),
            else_=input_valid_to,
        )
        rows = await session.exec(
            update(cls)
            .where(cls.id == inputs.c.id)
            .values(
                valid=case(
                    (backdated, cls.valid),
                    else_=func.tstzrange(lower, closing, "[)"),
                ),
                recorded=case(
                    (backdated, cls.recorded),
                    else_=func.tstzrange(cls.recorded.lower(result=datetime), func.now(), "[)"),
                ),
            )
            .returning(inputs.c.ordinal, valid_to)
            .execution_options(**{settings.skip_live_gate: True})
        )
        return dict(rows.all())

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
                func.now() - cls.last_accessed.coalesce(cls.recorded.lower(result=datetime)),
            )
            / timedelta(days=1).total_seconds()
            / half_life_days
        )
        relevance = func.power(literal(0.5, Float), half_lives) * (1 + cls.access_count)
        result = await session.exec(
            update(cls)
            .where(
                cls.recorded.f.upper_inf(result=bool),
                or_(cls.valid.is_(None), cls.valid.contains(func.now())),
                cls.scopes == sorted(scopes),
                relevance < floor,
            )
            .values(
                recorded=func.tstzrange(cls.recorded.lower(result=datetime), func.now()),
                attributes=cls.attributes.op("||")(func.jsonb_build_object("decayed", func.now())),
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
        now = datetime.now(UTC)
        now_ts = literal(now, DateTime(timezone=True))
        result = await session.exec(
            update(cls)
            .where(
                cls.recorded.f.upper_inf(result=bool),
                cls.source_chunk_id.in_(
                    select(Chunk.id).where(Chunk.document_id.in_(document_ids))
                ),
            )
            .values(
                recorded=func.tstzrange(cls.recorded.lower(result=datetime), now_ts),
                attributes=cls.attributes.op("||")(
                    func.jsonb_build_object(reason, literal(now.isoformat(), Text))
                ),
            )
            .returning(cls.id)
            .execution_options(synchronize_session=False)
        )
        return [row[0] for row in result]

    @classmethod
    async def forget_from_documents(
        cls, session: Session, document_ids: list[UUID7]
    ) -> list[UUID7]:
        """Retract live claims derived from explicitly forgotten documents."""
        return await cls.retract_from_documents(session, document_ids, "forgotten")


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
