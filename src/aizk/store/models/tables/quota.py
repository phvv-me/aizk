from datetime import date
from typing import ClassVar, Literal

import rls
from patos import sql
from patos.sql import NonNegativeInt
from pydantic import UUID5
from sqlalchemy import CheckConstraint, Date, Integer, String, Uuid, literal
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql.dml import ReturningInsert
from sqlmodel import select

from ...mixins import TableBase

type QuotaKind = Literal["operation", "remember", "web"]


class MonthlyQuotaCounter(TableBase, table=True):
    """One atomic monthly count shared by all stateless serving processes."""

    mutable: ClassVar[bool] = True
    __rls__ = rls.Open()
    __table_args__ = (
        CheckConstraint("used >= 0", name="ck_monthly_quota_counter_used_nonnegative"),
    )

    subject_id = sql.Field(UUID5, primary_key=True)
    period = sql.Field(date, primary_key=True)
    kind = sql.Field(str, primary_key=True, max_length=16)
    used = sql.Field(NonNegativeInt, default=0)

    @classmethod
    def consume(
        cls,
        subject_id: UUID5,
        period: date,
        kind: QuotaKind,
        limit: int,
        units: int = 1,
    ) -> ReturningInsert[int]:
        """Consume `units` of one counter only while its configured limit has room.

        A unit is what the operation really costs, which for an external provider call is
        the credits that provider charges rather than the one call the caller made. The
        guard leaves exactly enough room for the whole amount, so a limit is never crossed
        by an operation that happened to be expensive.

        The first call of a month has no row to conflict with, so the amount is selected
        rather than listed and the same room check gates the insert itself. A plain `VALUES`
        clause takes no predicate, which would have let one oversized first call spend a
        whole month's allowance and more before any counter existed to stop it.
        """
        opening = select(
            literal(subject_id, Uuid()),
            literal(period, Date()),
            literal(kind, String()),
            literal(units, Integer()),
        ).where(literal(units) <= literal(limit))
        return (
            insert(cls)
            .from_select(["subject_id", "period", "kind", "used"], opening)
            .on_conflict_do_update(
                index_elements=["subject_id", "period", "kind"],
                set_={"used": cls.used + units},
                where=cls.used <= limit - units,
            )
            .returning(cls.used)
        )
