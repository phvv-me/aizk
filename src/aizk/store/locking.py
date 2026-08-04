from collections.abc import Iterable

from pydantic import UUID7
from sqlalchemy import func, literal
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import select

from ..config import DatabaseBackend, settings
from .engine import Session
from .models.tables.coordination_lock import CoordinationLock


def document_revision(document_id: UUID7) -> str:
    """The lock every writer of one document's revision holds until it commits.

    A document's spans and its content hash change together, so any two writers of the same
    document must be serialized rather than merely consistent on their own. Ingestion takes
    this lock before it replaces a source's chunks and sharing takes it before it reads
    them, which is what keeps a move from copying spans that a concurrent re-ingest has
    already replaced and then retiring the source that now holds the newer text. Keying on
    the document alone rather than on a destination is deliberate: a second share into a
    different scope set reads the same spans and belongs in the same queue.
    """
    return f"document|{document_id}"


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
