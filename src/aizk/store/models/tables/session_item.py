from datetime import datetime, timedelta
from typing import ClassVar, Self

from patos import sql
from sqlalchemy import ColumnElement, Index, String, UniqueConstraint, func, or_
from sqlalchemy.orm import declared_attr
from sqlmodel import select
from sqlmodel.sql.expression import SelectOfScalar

from ....types import Scopes
from ...mixins import Embedded, Id, Scoped, TableBase, Timestamped


class SessionItem(Id, Scoped, Timestamped, Embedded, TableBase, table=True):
    """Fast working-memory item awaiting long-term graph promotion."""

    mutable: ClassVar[bool] = True

    kind = sql.Field(str, default="note", sa_type=String, server_default=None)
    text = sql.Field(str)
    provenance = sql.Field(dict, default_factory=dict, sa_type=sql.TypedJSONB)
    promoted_at = sql.Field(
        datetime | None,
        index=True,
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index | UniqueConstraint, ...]:
        return (
            *super().__table_args__,
            Index("ix_session_item_scopes", "scopes", postgresql_using="gin"),
        )

    @classmethod
    def line(cls) -> ColumnElement[str]:
        """The item's `- [kind] speaker: text` evidence line, the speaker prefix only
        when provenance recorded one."""
        speaker_label = cls.provenance >> "speaker_label"
        speaker = sql.fragment(t"{speaker_label}: ")
        kind, text = cls.kind, cls.text
        return sql.concat(t"- [{kind}] {speaker}{text}")

    @classmethod
    def due_for_promotion(
        cls, scopes: Scopes, age_minutes: float, threshold: int
    ) -> SelectOfScalar[Self]:
        """Select the aged and overflow working items oldest first, decided in the database.

        An item is due once it outlives `age_minutes`, and the oldest items past the
        `threshold` count spill over regardless of age; the window functions rank one pass
        over the still-working items in the exact scope set.
        """
        ranked = (
            select(
                cls,
                func.row_number().over(order_by=cls.created_at).label("position"),
                cls.id.count().over().label("total"),
            )
            .where(cls.promoted_at.is_(None), cls.scopes == sorted(scopes))
            .subquery("working")
        )
        working = ranked.c
        age = timedelta(minutes=age_minutes)
        aged = working.created_at <= func.now() - age
        overflow = working.position <= working.total - threshold
        return (
            select(cls)
            .join(ranked, working.id == cls.id)
            .where(or_(aged, overflow))
            .order_by(working.created_at)
        )
