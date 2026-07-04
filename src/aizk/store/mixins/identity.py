import uuid
from datetime import datetime
from typing import cast

from sqlalchemy import DateTime, func
from sqlmodel import Field


class Id:
    """A client-generated uuid primary key, the one surrogate-id strategy every table but the
    identity join tables shares.

    uuid7 over uuid4: its leading bits carry a millisecond timestamp, so ids generated close in
    time sort close together, keeping new rows landing at one edge of the primary key's b-tree
    instead of a uuid4's fully random insert point scattering writes across the whole index.

    id: stable identity, generated client-side on insert unless the caller passes its own
        content-addressed uuid5, as entities and facts do.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid7, primary_key=True)


class Timestamped:
    """A first-seen `created_at` and a last-write `updated_at`, both server-stamped.

    Both fields build their columns from `sa_column_kwargs`, never a literal
    `sa_column=Column(...)`, so every subclass gets its own fresh `Column` rather than sharing
    one physical instance across tables.

    created_at: first-seen timestamp, stamped on insert and never touched again.
    updated_at: last-write timestamp, stamped on insert and bumped on every ORM-flush `UPDATE`
        (SQLAlchemy's `onupdate`), so a raw upsert that never goes through the ORM's own
        `UPDATE`, `Watermark`'s `on_conflict_do_update` set clause, still has to stamp
        `"updated_at": func.now()` itself.
    """

    created_at: datetime = Field(
        default=None,
        nullable=False,
        sa_type=cast(type[datetime], DateTime(timezone=True)),
        sa_column_kwargs={"server_default": func.now()},
    )
    updated_at: datetime = Field(
        default=None,
        nullable=False,
        sa_type=cast(type[datetime], DateTime(timezone=True)),
        sa_column_kwargs={"server_default": func.now(), "onupdate": func.now()},
    )
