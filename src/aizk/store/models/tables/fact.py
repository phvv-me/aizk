import uuid
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from sqlalchemy import (
    Boolean,
    ColumnElement,
    DateTime,
    Float,
    Index,
    Table,
    Text,
    and_,
    cast,
    extract,
    func,
    or_,
    update,
)
from sqlalchemy import Column as SAColumn
from sqlalchemy.dialects.postgresql import TSTZRANGE, Range
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import declared_attr
from sqlmodel import Field, select

from ....common.sql import Column, TypedJSONB
from ....types import Scopes
from ...engine import Session
from ...mixins import ClaimedContent, Embedded, Id, Scoped, TableBase
from .chunk import Chunk


class FactClaim(Id, Scoped, TableBase, table=True):
    """A scope set's bi-temporal and behavioral claim on shared fact content."""

    mutable: ClassVar[bool] = True

    content_id: Column[uuid.UUID] = Field(
        foreign_key="fact_content.id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )
    valid: Column[Range[datetime] | None] = Field(default=None, sa_column=SAColumn(TSTZRANGE))
    recorded: Column[Range[datetime]] = Field(
        default=None,
        sa_column=SAColumn(
            TSTZRANGE,
            nullable=False,
            server_default=func.tstzrange(func.now(), None, "[)"),
        ),
    )
    last_accessed: Column[datetime | None] = Field(
        default=None,
        sa_column=SAColumn(DateTime(timezone=True)),
    )
    access_count: Column[int] = Field(default=0, sa_column_kwargs={"server_default": "0"})
    attributes: Column[dict] = Field(
        default_factory=dict,
        sa_column_kwargs={"server_default": "{}"},
        sa_type=TypedJSONB,
    )
    perspective_key: Column[str] = Field(
        default="world",
        index=True,
        sa_column_kwargs={"server_default": "world"},
    )
    source_chunk_id: Column[uuid.UUID | None] = Field(
        default=None,
        foreign_key="chunk.id",
        ondelete="SET NULL",
        index=True,
    )
    promoted_from: Column[uuid.UUID | None] = Field(
        default=None,
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
            func.upper_inf(cls.recorded, type_=Boolean),
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
        return func.lower(cls.recorded)

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
    async def record_access(cls, session: Session, claim_ids: list[uuid.UUID]) -> None:
        """Update recency and frequency for surfaced live facts in one statement."""
        if not claim_ids:
            return
        await session.exec(
            update(cls)
            .where(
                func.upper_inf(cls.recorded, type_=Boolean),
                cls.id.in_(claim_ids),
            )
            .values(last_accessed=func.now(), access_count=cls.access_count + 1)
            .execution_options(synchronize_session=False)
        )

    @classmethod
    async def archive_stale(
        cls,
        session: Session,
        scopes: Scopes,
        half_life_days: float,
        floor: float,
    ) -> list[uuid.UUID]:
        """Close live claims below the relevance floor and return their IDs.

        The clock, the range close, and the decay stamp all come from the database's own
        now(), so one statement decides staleness with no host-side time conversion.
        """
        half_lives = (
            extract(
                "epoch",
                func.now() - func.coalesce(cls.last_accessed, func.lower(cls.recorded)),
            )
            / timedelta(days=1).total_seconds()
            / half_life_days
        )
        relevance = func.power(cast(0.5, Float), half_lives) * (1 + cls.access_count)
        result = await session.exec(
            update(cls)
            .where(
                func.upper_inf(cls.recorded, type_=Boolean),
                or_(cls.valid.is_(None), cls.valid.contains(func.now())),
                cls.scopes == sorted(scopes),
                relevance < floor,
            )
            .values(
                recorded=func.tstzrange(func.lower(cls.recorded), func.now()),
                attributes=cls.attributes.op("||")(func.jsonb_build_object("decayed", func.now())),
            )
            .returning(cls.id)
            .execution_options(synchronize_session=False)
        )
        return list(result.scalars().all())

    @classmethod
    async def retract_from_documents(
        cls,
        session: Session,
        document_ids: list[uuid.UUID],
        reason: str,
    ) -> list[uuid.UUID]:
        """Close live claims derived from documents before their chunks change."""
        now = datetime.now(UTC)
        now_ts = cast(now, DateTime(timezone=True))
        result = await session.exec(
            update(cls)
            .where(
                func.upper_inf(cls.recorded, type_=Boolean),
                cls.source_chunk_id.in_(
                    select(Chunk.id).where(Chunk.document_id.in_(document_ids))
                ),
            )
            .values(
                recorded=func.tstzrange(func.lower(cls.recorded), now_ts),
                attributes=cls.attributes.op("||")(
                    func.jsonb_build_object(reason, cast(now.isoformat(), Text))
                ),
            )
            .returning(cls.id)
            .execution_options(synchronize_session=False)
        )
        return list(result.scalars().all())

    @classmethod
    async def forget_from_documents(
        cls, session: Session, document_ids: list[uuid.UUID]
    ) -> list[uuid.UUID]:
        """Retract live claims derived from explicitly forgotten documents."""
        return await cls.retract_from_documents(session, document_ids, "forgotten")


class FactContent(Id, Embedded, ClaimedContent, TableBase, table=True):
    """Immutable, content-addressed graph edge shared by visible claims."""

    subject_id: Column[uuid.UUID] = Field(
        foreign_key="entity_content.id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )
    object_id: Column[uuid.UUID | None] = Field(
        default=None,
        foreign_key="entity_content.id",
        ondelete="CASCADE",
        index=True,
    )
    predicate: Column[str] = Field(sa_type=Text, foreign_key="relation_kind.name")
    statement: Column[str] = Field(sa_type=Text)
    claim_table: ClassVar[Table] = FactClaim.__table__
