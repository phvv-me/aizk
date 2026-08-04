from datetime import datetime
from enum import auto
from typing import ClassVar, Self, cast

from patos import sql
from patos.sql import NonNegativeFloat, NonNegativeInt
from pydantic import UUID5
from sqlalchemy import BigInteger, CheckConstraint, ColumnElement, Index, Label, Uuid, func, or_
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import ARRAY, Insert, insert
from sqlmodel import select
from sqlmodel.sql.expression import Select

from ...mixins import CreatedAt, Id, Scoped, TableBase
from .quota import MonthlyQuotaCounter


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
        Index("uq_usage_event_capture_key", "capture_key", unique=True),
        Index("ix_usage_event_scopes", "scopes", postgresql_using="gin"),
        Index("ix_usage_event_targets", "targets", postgresql_using="gin"),
    )

    capture_key = sql.Field(str, max_length=64)
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
    def report_totals(cls, start: datetime | None = None) -> Select[tuple[int, ...]]:
        """Aggregate operation, item, byte, and duration totals after an optional instant."""
        statement = select(*cls.aggregate()).add_columns(
            cls.id.count().label("requests"),
            cls.integer_sum(func.sum(cls.items), "items"),
            cls.duration_ms.sum(default=0.0).label("duration_ms"),
        )
        if start is not None:
            statement = statement.where(cls.created_at >= start)
        return cast("Select[tuple[int, ...]]", statement)

    @classmethod
    def daily_since(
        cls, start: datetime
    ) -> Select[tuple[datetime, str, int, int, int, int, float]]:
        """Daily operation buckets recorded on or after one UTC instant."""
        bucket = func.date_trunc("day", cls.created_at)
        return cast(
            "Select[tuple[datetime, str, int, int, int, int, float]]",
            select(
                bucket.label("bucket"),
                cls.operation,
                cls.id.count().label("requests"),
            )
            .add_columns(
                cls.integer_sum(func.sum(cls.items), "items"),
                cls.integer_sum(func.sum(cls.request_bytes), "request_bytes"),
                cls.integer_sum(func.sum(cls.response_bytes), "response_bytes"),
                cls.duration_ms.sum(default=0.0).label("duration_ms"),
            )
            .where(cls.created_at >= start)
            .group_by(bucket, cls.operation)
            .order_by(bucket, cls.operation),
        )

    @classmethod
    def capture(cls, event: Self) -> Insert:
        """Build one idempotent durable usage capture."""
        return (
            insert(cls)
            .values(event.model_dump())
            .on_conflict_do_nothing(index_elements=["capture_key"])
        )

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
            cls.integer_sum(func.sum(cls.request_bytes), "request_bytes"),
            cls.integer_sum(func.sum(cls.response_bytes), "response_bytes"),
            cls.integer_sum(
                func.sum(cls.request_bytes).filter(operation == cls.Operation.remember_file),
                "uploaded_bytes",
            ),
            cls.integer_sum(
                func.sum(cls.response_bytes).filter(operation == cls.Operation.artifact_read),
                "downloaded_bytes",
            ),
        )

    @staticmethod
    def integer_sum(expression: ColumnElement[int], label: str) -> Label[int]:
        """Cast dialect-specific integer sums back to stable signed 64-bit report values."""
        return func.coalesce(sql_cast(expression, BigInteger()), 0).label(label)


class Usage:
    """Namespace for durable usage accounting models."""

    Event: ClassVar[type[UsageEvent]] = UsageEvent
    MonthlyQuota: ClassVar[type[MonthlyQuotaCounter]] = MonthlyQuotaCounter
