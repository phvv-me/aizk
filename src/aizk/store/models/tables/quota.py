from datetime import date
from typing import ClassVar, Literal

import rls
from patos import sql
from patos.sql import NonNegativeInt
from pydantic import UUID5
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql.dml import ReturningInsert

from ...mixins import TableBase

type QuotaKind = Literal["operation", "remember"]


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
    ) -> ReturningInsert[tuple[int]]:
        """Increment one counter only while its configured limit has room."""
        return (
            insert(cls)
            .values(subject_id=subject_id, period=period, kind=kind, used=1)
            .on_conflict_do_update(
                index_elements=["subject_id", "period", "kind"],
                set_={"used": cls.used + 1},
                where=cls.used < limit,
            )
            .returning(cls.used)
        )
