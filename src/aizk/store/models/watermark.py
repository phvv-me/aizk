import uuid
from datetime import datetime
from enum import StrEnum, auto

from sqlalchemy import BigInteger, Column, DateTime, Text, UniqueConstraint, func, select
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field

from ..mixins import Id, Scoped, TableBase

# the single ref every non-entity watermark is keyed under, since those counters are graph-wide
GLOBAL = "global"


class Watermark(Id, Scoped, TableBase, table=True):
    """A tiny per-principal counter the autonomous engine debounces and gates its passes against.

    Where the other scoped tables hold memory, this one holds bookkeeping. The on-write path bumps
    an `entity_dirty` row per touched entity so a debounced profile rebuild knows the portrait went
    stale, the community pass reads a `fact_count` row to skip a graph that has not grown past the
    threshold since its last summary, and the self-improve pass stores its latest `scorecard` here
    as a payload. One row per owner, kind, and ref, scoped and row-level-security forced exactly
    like the memory it tracks so a counter never leaks across principals.

    id: stable identity, generated client-side on insert.
    owner_id: principal that owns the row, enforced by row level security.
    scope: group the row is shared with, always null since a watermark is private bookkeeping.
    kind: discriminator naming what the row tracks, such as entity_dirty, fact_count, or scorecard.
    ref: the subject the counter is keyed to, an entity id for entity_dirty or global otherwise.
    counter: the integer the kind accumulates, a dirty count or a high-water fact count.
    payload: free-form structured detail, where the self-improve scorecard is stored.
    updated_at: last time the row was bumped.
    """

    class Kind(StrEnum):
        """The bookkeeping a watermark row tracks."""

        entity_dirty = auto()
        fact_count = auto()
        raptor_fact_count = auto()
        curation_pending = auto()
        scorecard = auto()
        config = auto()

    __table_args__ = (UniqueConstraint("owner_id", "kind", "ref"),)

    kind: Kind = Field(sa_column=Column(SAEnum(Kind, name="watermark_kind"), nullable=False))
    ref: str = Field(default="global", sa_type=Text)
    counter: int = Field(default=0, sa_column_kwargs={"server_default": "0"}, sa_type=BigInteger)
    payload: dict = Field(
        default_factory=dict, sa_column_kwargs={"server_default": "{}"}, sa_type=JSONB
    )
    updated_at: datetime = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
        ),
    )

    @classmethod
    async def bump(
        cls,
        session: AsyncSession,
        owner_id: uuid.UUID,
        kind: Watermark.Kind,
        ref: str = GLOBAL,
        by: int = 1,
    ) -> int:
        """Increment one watermark counter, inserting the row on first sight, return the new value.

        A single upsert on the owner, kind, and ref unique key, so concurrent extractions of the
        same entity accumulate rather than race, and a counter that does not exist yet starts
        from `by`.

        session: open session already acting as owner_id under row level security.
        owner_id: principal that owns the counter.
        kind: discriminator naming what the counter tracks.
        ref: subject the counter is keyed to, the entity id for a dirty count, global otherwise.
        by: amount to add to the counter.
        """
        statement = (
            insert(cls)
            .values(owner_id=owner_id, kind=kind, ref=ref, counter=by)
            .on_conflict_do_update(
                index_elements=["owner_id", "kind", "ref"],
                set_={"counter": cls.counter + by, "updated_at": func.now()},
            )
            .returning(cls.counter)
        )
        return await session.scalar(statement) or 0

    @classmethod
    async def read(
        cls, session: AsyncSession, owner_id: uuid.UUID, kind: Watermark.Kind, ref: str = GLOBAL
    ) -> int:
        """Read one watermark counter, zero when the row does not exist yet.

        session: open session already acting as owner_id under row level security.
        owner_id: principal that owns the counter.
        kind: discriminator naming what the counter tracks.
        ref: subject the counter is keyed to.
        """
        value = await session.scalar(
            select(cls.counter)
            .where(cls.owner_id == owner_id)
            .where(cls.kind == kind)
            .where(cls.ref == ref)
        )
        return value or 0

    @classmethod
    async def read_payload(
        cls, session: AsyncSession, owner_id: uuid.UUID, kind: Watermark.Kind, ref: str = GLOBAL
    ) -> dict:
        """Read one watermark's payload, an empty object when the row does not exist yet.

        The payload counterpart of `read`, so recall can read back the live config the
        self-improve pass flipped and a caller can recover any structured detail a kind stored.

        session: open session already acting as owner_id under row level security.
        owner_id: principal that owns the counter.
        kind: discriminator naming what the row tracks.
        ref: subject the counter is keyed to.
        """
        payload = await session.scalar(
            select(cls.payload)
            .where(cls.owner_id == owner_id)
            .where(cls.kind == kind)
            .where(cls.ref == ref)
        )
        return payload or {}

    @classmethod
    async def set_value(
        cls,
        session: AsyncSession,
        owner_id: uuid.UUID,
        kind: Watermark.Kind,
        counter: int = 0,
        payload: dict | None = None,
        ref: str = GLOBAL,
    ) -> None:
        """Upsert a watermark to an absolute counter and payload, the high-water/scorecard writer.

        Where `bump` accumulates, this sets the row outright, so the community pass can record the
        fact count it summarized at and the self-improve pass can store its latest scorecard
        payload.

        session: open session already acting as owner_id under row level security.
        owner_id: principal that owns the counter.
        kind: discriminator naming what the counter tracks.
        counter: the absolute value to store.
        payload: the structured detail to store, an empty object when null.
        ref: subject the counter is keyed to.
        """
        values = {"counter": counter, "payload": payload or {}}
        statement = (
            insert(cls)
            .values(owner_id=owner_id, kind=kind, ref=ref, **values)
            .on_conflict_do_update(
                index_elements=["owner_id", "kind", "ref"],
                set_={**values, "updated_at": func.now()},
            )
        )
        await session.execute(statement)
