import uuid
from collections.abc import Sequence
from enum import StrEnum, auto
from typing import ClassVar, cast

from sqlalchemy import BigInteger, Index, Text, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Field, select

from ....common.sql import Column, TypedJSONB
from ....config import settings
from ....types import Scopes
from ...engine import Session
from ...mixins import Id, Scoped, TableBase, Timestamped

_GLOBAL_REF = "global"


class Watermark(Id, Scoped, Timestamped, TableBase, table=True):
    """Per-scope counter and payload for autonomous maintenance passes."""

    mutable: ClassVar[bool] = True

    class Kind(StrEnum):
        """Maintenance state tracked by a watermark."""

        entity_dirty = auto()
        fact_count = auto()
        raptor_fact_count = auto()
        curation_pending = auto()
        scorecard = auto()
        config = auto()

    __table_args__ = (
        Index("ix_watermark_scopes", "scopes", postgresql_using="gin"),
        UniqueConstraint("scopes", "kind", "ref", name="uq_watermark_scope_kind_ref"),
    )

    kind: Column[Kind] = Field(
        nullable=False,
        sa_type=cast(type[Kind], SAEnum(Kind, name="watermark_kind")),
    )
    ref: Column[str] = Field(default=_GLOBAL_REF, sa_type=Text)
    counter: Column[int] = Field(
        default=0,
        sa_column_kwargs={"server_default": "0"},
        sa_type=BigInteger,
    )
    payload: Column[dict] = Field(
        default_factory=dict,
        sa_column_kwargs={"server_default": "{}"},
        sa_type=TypedJSONB,
    )

    @classmethod
    async def bump(
        cls,
        session: Session,
        scopes: Scopes,
        kind: Watermark.Kind,
        ref: str = _GLOBAL_REF,
        by: int = 1,
        created_by: uuid.UUID | None = None,
    ) -> int:
        """Atomically increment a counter and return its new value."""
        statement = (
            insert(cls)
            .values(
                created_by=created_by or settings.system_user_id,
                scopes=sorted(scopes),
                kind=kind,
                ref=ref,
                counter=by,
            )
            .on_conflict_do_update(
                index_elements=["scopes", "kind", "ref"],
                set_={"counter": cls.counter + by, "updated_at": func.now()},
            )
            .returning(cls.counter)
        )
        return (await session.exec(statement)).scalar_one()

    @classmethod
    async def bump_many(
        cls,
        session: Session,
        scopes: Scopes,
        kind: Watermark.Kind,
        refs: Sequence[str],
        by: int = 1,
        created_by: uuid.UUID | None = None,
    ) -> None:
        """Atomically increment many referenced counters in one statement."""
        if not refs:
            return
        statement = insert(cls).values(
            [
                {
                    "created_by": created_by or settings.system_user_id,
                    "scopes": sorted(scopes),
                    "kind": kind,
                    "ref": ref,
                    "counter": by,
                }
                for ref in dict.fromkeys(refs)
            ]
        )
        await session.exec(
            statement.on_conflict_do_update(
                index_elements=["scopes", "kind", "ref"],
                set_={
                    "counter": cls.counter + statement.excluded.counter,
                    "updated_at": func.now(),
                },
            )
        )

    @classmethod
    async def read(
        cls,
        session: Session,
        scopes: Scopes,
        kind: Watermark.Kind,
        ref: str = _GLOBAL_REF,
    ) -> int:
        """Read a counter or zero when it has not been created."""
        value = (
            await session.exec(
                select(cls.counter).where(
                    cls.scopes == sorted(scopes),
                    cls.kind == kind,
                    cls.ref == ref,
                )
            )
        ).first()
        return value or 0

    @classmethod
    async def read_payload(
        cls,
        session: Session,
        scopes: Scopes,
        kind: Watermark.Kind,
        ref: str = _GLOBAL_REF,
    ) -> dict:
        """Read a payload or an empty mapping when absent."""
        payload = (
            await session.exec(
                select(cls.payload).where(
                    cls.scopes == sorted(scopes),
                    cls.kind == kind,
                    cls.ref == ref,
                )
            )
        ).first()
        return payload or {}

    @classmethod
    async def set_value(
        cls,
        session: Session,
        scopes: Scopes,
        kind: Watermark.Kind,
        counter: int = 0,
        payload: dict | None = None,
        ref: str = _GLOBAL_REF,
        created_by: uuid.UUID | None = None,
    ) -> None:
        """Atomically replace a counter and payload."""
        values = {"counter": counter, "payload": payload or {}}
        statement = (
            insert(cls)
            .values(
                created_by=created_by or settings.system_user_id,
                scopes=sorted(scopes),
                kind=kind,
                ref=ref,
                **values,
            )
            .on_conflict_do_update(
                index_elements=["scopes", "kind", "ref"],
                set_={**values, "updated_at": func.now()},
            )
        )
        await session.exec(statement)
