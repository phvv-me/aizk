from datetime import datetime
from typing import ClassVar, Self, cast

from patos import sql
from sqlalchemy import ColumnElement, DateTime, Index, Text, UniqueConstraint, func, or_, select
from sqlalchemy.orm import declared_attr
from sqlalchemy.sql.selectable import Select
from sqlmodel import Field

from ....types import Scopes
from ...mixins import Embedded, Id, Scoped, TableBase, Timestamped


class SessionItem(Id, Scoped, Timestamped, Embedded, TableBase, table=True):
    """Fast working-memory item awaiting long-term graph promotion."""

    mutable: ClassVar[bool] = True

    kind: sql.Column[str] = Field(default="note")
    text: sql.Column[str] = Field(sa_type=Text)
    provenance: sql.Column[dict] = Field(
        default_factory=dict, sa_type=sql.TypedJSONB, sa_column_kwargs={"server_default": "{}"}
    )
    promoted_at: sql.Column[datetime | None] = Field(
        default=None, index=True, sa_type=cast(type[datetime], DateTime(timezone=True))
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
    def due_for_promotion(cls, scopes: Scopes, age_minutes: float, threshold: int) -> Select[Self]:
        """Select the aged and overflow working items oldest first, decided in the database.

        An item is due once it outlives `age_minutes`, and the oldest items past the
        `threshold` count spill over regardless of age; the window functions rank one pass
        over the still-working items in the exact scope set.
        """
        ranked = (
            select(
                cls,
                func.row_number().over(order_by=cls.created_at).label("position"),
                func.count().over().label("total"),
            )
            .where(cls.promoted_at.is_(None), cls.scopes == sorted(scopes))
            .subquery("working")
        )
        working = ranked.c
        # make_interval only accepts a fractional value in its seconds slot.
        age = func.make_interval(0, 0, 0, 0, 0, 0, age_minutes * 60.0)
        aged = working.created_at <= func.now() - age
        overflow = working.position <= working.total - threshold
        return (
            select(cls)
            .join(ranked, working.id == cls.id)
            .where(or_(aged, overflow))
            .order_by(working.created_at)
        )
