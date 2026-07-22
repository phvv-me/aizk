from datetime import datetime

from patos import FrozenModel
from sqlmodel import select

from ..config import settings
from ..store import Chunk
from ..store.identity import User
from .queue import Queue


class TasksStatus(FrozenModel):
    """Bounded operational snapshot of the durable queue and projection backlog."""

    pending: int
    running: int
    failed: int
    last_success: str | None
    oldest_queued: str | None
    projection_pending: int

    @staticmethod
    def stamp(value: datetime | None) -> str | None:
        """Serialize one optional queue timestamp in UTC-aware ISO form."""
        return value.isoformat() if value is not None else None


async def tasks_overview() -> TasksStatus:
    """Read bounded queue aggregates and the authoritative pending-chunk count."""
    async with Queue(dsn=settings.asyncpg_dsn) as queue:
        snapshot = await queue.snapshot()
    async with User.system().owner as session:
        projection_pending = (
            await session.exec(select(Chunk.id.count()).where(Chunk.processed_at.is_(None)))
        ).one()
    return TasksStatus(
        pending=snapshot.pending,
        running=snapshot.running,
        failed=snapshot.failed,
        last_success=TasksStatus.stamp(snapshot.last_success),
        oldest_queued=TasksStatus.stamp(snapshot.oldest_queued),
        projection_pending=projection_pending,
    )
