from enum import auto
from typing import ClassVar, cast

from patos import sql
from patos.sql import NonNegativeFloat, NonNegativeInt
from pydantic import UUID5
from sqlalchemy import CheckConstraint, Index, Label, Uuid, func, or_
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import select
from sqlmodel.sql.expression import Select

from ...mixins import CreatedAt, Id, Scoped, TableBase


class UsageEvent(Id, Scoped, CreatedAt, TableBase, table=True):
    """One immutable successful operation for durable cost and quota accounting."""

    class Operation(sql.PGEnum):
        """Public AIZK operations whose resource use needs attribution."""

        recall = auto()
        remember_text = auto()
        remember_file = auto()
        share = auto()
        artifact_read = auto()

    mutable: ClassVar[bool] = False

    __table_args__ = (
        CheckConstraint("request_bytes >= 0", name="ck_usage_request_bytes_nonnegative"),
        CheckConstraint("response_bytes >= 0", name="ck_usage_response_bytes_nonnegative"),
        CheckConstraint("items >= 0", name="ck_usage_items_nonnegative"),
        CheckConstraint("duration_ms >= 0", name="ck_usage_duration_ms_nonnegative"),
        Index("ix_usage_event_scopes", "scopes", postgresql_using="gin"),
        Index("ix_usage_event_targets", "targets", postgresql_using="gin"),
    )

    operation = sql.Field(Operation, index=True)
    targets = sql.Field(list[UUID5], sa_type=ARRAY(Uuid()))
    request_bytes = sql.Field(NonNegativeInt, default=0)
    response_bytes = sql.Field(NonNegativeInt, default=0)
    items = sql.Field(NonNegativeInt, default=1)
    duration_ms = sql.Field(NonNegativeFloat, default=0.0)

    @classmethod
    def totals(cls) -> Select[tuple[int, ...]]:
        """One aggregate row of the shared per-operation usage report columns."""
        return cast("Select[tuple[int, ...]]", select(*cls.aggregate()))

    @classmethod
    def aggregate(cls) -> tuple[Label[int], ...]:
        """The shared per-operation count and byte aggregates every usage report reads."""
        operation = cls.operation
        remembered = or_(
            operation == cls.Operation.remember_text,
            operation == cls.Operation.remember_file,
        )
        return (
            cls.id.count().filter(operation == cls.Operation.recall).label("recalls"),
            cls.id.count().filter(remembered).label("remembers"),
            cls.id.count().filter(operation == cls.Operation.remember_file).label("files"),
            cls.id.count().filter(operation == cls.Operation.share).label("shares"),
            cls.id.count()
            .filter(operation == cls.Operation.artifact_read)
            .label("artifact_reads"),
            cls.request_bytes.sum(default=0).label("request_bytes"),
            cls.response_bytes.sum(default=0).label("response_bytes"),
            func.coalesce(
                func.sum(cls.request_bytes).filter(operation == cls.Operation.remember_file), 0
            ).label("uploaded_bytes"),
            func.coalesce(
                func.sum(cls.response_bytes).filter(operation == cls.Operation.artifact_read), 0
            ).label("downloaded_bytes"),
        )


class Usage:
    """Namespace for durable usage accounting models."""

    Event: ClassVar[type[UsageEvent]] = UsageEvent
