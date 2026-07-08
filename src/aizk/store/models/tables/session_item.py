from datetime import datetime, timedelta
from typing import Self, cast

from sqlalchemy import DateTime, Text
from sqlmodel import Field

from ...mixins import Embedded, Id, Scoped, TableBase, Timestamped


class SessionItem(Id, Scoped, Timestamped, Embedded, TableBase, table=True):
    """One fast working-memory item a user remembered, before it reaches the long-term graph.

    The session tier is the cheap front of memory. A remember writes a single embedded row here
    rather than paying the chunk, embed, and extract pipeline up front, so a capture is immediate.
    A recall reads the still-working items alongside the graph, and the promotion pass later feeds
    the aged or overflow items through the on-write pipeline that extracts and consolidates them,
    stamping promoted_at so they leave the working set once their knowledge lives in the graph. The
    row is scoped and row-level-security forced exactly like the memory it becomes.

    id: stable identity, generated client-side on insert.
    owner_id: user that owns the row, enforced by row level security.
    scopes: group set the row is shared with, empty when private to the owner.
    kind: coarse type tag carried through to the promoted document, such as note or code.
    text: the remembered content, ranked by its embedding and fed whole to promotion.
    embedding: halfvec dense vector of the text, what the session recall lane ranks.
    created_at: capture time, the age the promotion pass measures against.
    promoted_at: time the item was fed into the long-term graph, null while it is still working.
    """

    kind: str = Field(default="note")
    text: str = Field(sa_type=Text)
    promoted_at: datetime | None = Field(
        default=None, index=True, sa_type=cast(type[datetime], DateTime(timezone=True))
    )

    @classmethod
    def due_for_promotion(
        cls, items: list[Self], now: datetime, age_minutes: float, threshold: int
    ) -> list[Self]:
        """The working items to move into the graph, the aged ones plus any over the working cap.

        An item is due once it has aged past the cutoff, so knowledge settles into the graph on a
        steady cadence, and additionally the oldest items beyond the working threshold are due
        whatever their age, so the working set stays bounded under a burst of writes. The two sets
        union while keeping the oldest-first order the caller reads, so a single pass drains both
        triggers.

        items: the unpromoted working items, ordered oldest first.
        now: the moment the pass runs, the reference the age is measured from.
        age_minutes: age after which an item is promoted regardless of the working count.
        threshold: most unpromoted items the working set may hold before the oldest overflow.
        """
        cutoff = now - timedelta(minutes=age_minutes)
        overflow = max(0, len(items) - threshold)
        due = {
            item.id: item
            for index, item in enumerate(items)
            if item.created_at <= cutoff or index < overflow
        }
        return [item for item in items if item.id in due]
