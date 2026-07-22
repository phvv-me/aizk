from collections.abc import Iterable

from sqlalchemy import func, literal
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import select

from ..config import DatabaseBackend, settings
from .engine import Session
from .models.tables.coordination_lock import CoordinationLock


async def acquire_locks(session: Session, keys: Iterable[str]) -> None:
    """Acquire transaction-scoped locks in canonical order on either database backend."""
    ordered = sorted(set(keys))
    if settings.database_backend is DatabaseBackend.postgresql:
        for key in ordered:
            await session.exec(
                select(func.pg_advisory_xact_lock(func.hashtextextended(literal(key), 0)))
            )
        return
    for key in ordered:
        await session.exec(insert(CoordinationLock).values(key=key).on_conflict_do_nothing())
        await session.exec(
            select(CoordinationLock.key).where(CoordinationLock.key == key).with_for_update()
        )
