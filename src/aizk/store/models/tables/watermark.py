from collections.abc import Mapping, Sequence
from enum import auto
from typing import ClassVar

from patos import sql
from pydantic import UUID5
from sqlalchemy import BigInteger, Index, Text, UniqueConstraint, column, func, update
from sqlalchemy import Column as SAColumn
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Field, select

from ....config import settings
from ....types import Scopes
from ...engine import Session
from ...mixins import Id, Scoped, TableBase, Timestamped

_GLOBAL_REF = "global"


class Watermark(Id, Scoped, Timestamped, TableBase, table=True):
    """Per-scope counter and payload for autonomous maintenance passes."""

    class Kind(sql.PGEnum):
        """Maintenance state tracked by a watermark."""

        entity_dirty = auto()
        fact_count = auto()
        raptor_fact_count = auto()
        config = auto()

    mutable: ClassVar[bool] = True

    __table_args__ = (
        Index("ix_watermark_scopes", "scopes", postgresql_using="gin"),
        UniqueConstraint("scopes", "kind", "ref", name="uq_watermark_scope_kind_ref"),
    )

    kind: sql.Column[Kind] = Field(
        sa_column=SAColumn(Kind.type, nullable=False),
    )
    ref: sql.Column[str] = Field(default=_GLOBAL_REF, sa_type=Text)
    counter: sql.Column[int] = Field(
        default=0,
        sa_column_kwargs={"server_default": "0"},
        sa_type=BigInteger,
    )
    payload: sql.Column[dict] = Field(
        default_factory=dict,
        sa_column_kwargs={"server_default": "{}"},
        sa_type=sql.TypedJSONB,
    )

    @classmethod
    async def bump(
        cls,
        session: Session,
        scopes: Scopes,
        kind: Kind,
        ref: str = _GLOBAL_REF,
        by: int = 1,
        created_by: UUID5 | None = None,
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
        kind: Kind,
        refs: Sequence[str],
        by: int = 1,
        created_by: UUID5 | None = None,
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
        kind: Kind,
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
        kind: Kind,
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
    async def pending_refs(
        cls,
        session: Session,
        scopes: Scopes,
        kind: Kind,
        limit: int,
    ) -> dict[str, int]:
        """Read the oldest positive referenced counters as one bounded work batch."""
        rows = await session.exec(
            select(cls.ref, cls.counter)
            .where(
                cls.scopes == sorted(scopes),
                cls.kind == kind,
                cls.counter > 0,
            )
            .order_by(cls.updated_at, cls.ref)
            .limit(limit)
        )
        return dict(rows.all())

    @classmethod
    async def consume(
        cls,
        session: Session,
        scopes: Scopes,
        kind: Kind,
        counters: Mapping[str, int],
    ) -> None:
        """Subtract one processed snapshot without erasing concurrent increments."""
        if not counters:
            return
        consumed = sql.relation(
            "consumed_watermark",
            (
                column("ref", Text),
                column("counter", BigInteger),
            ),
            list(counters.items()),
        )
        await session.exec(
            update(cls)
            .where(
                cls.scopes == sorted(scopes),
                cls.kind == kind,
                cls.ref == consumed.c.ref,
            )
            .values(
                counter=func.greatest(cls.counter - consumed.c.counter, 0),
                updated_at=func.now(),
            )
        )

    @classmethod
    async def set_value(
        cls,
        session: Session,
        scopes: Scopes,
        kind: Kind,
        counter: int = 0,
        payload: dict | None = None,
        ref: str = _GLOBAL_REF,
        created_by: UUID5 | None = None,
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
