from collections import defaultdict
from datetime import datetime

from patos import FrozenModel
from sqlmodel import select

from ..config import settings
from ..store import Chunk
from ..store.identity import User
from .enum import QueueStatus
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
        sizes = await queue.queries.queue_size()
        names = queue.queries.qbe.settings
        last_success = await queue.connection.fetchval(
            f"SELECT max(created) FROM {names.queue_table_log} WHERE status = 'successful'"
        )
        oldest_queued = await queue.connection.fetchval(
            f"SELECT min(created) FROM {names.queue_table} WHERE status = 'queued'"
        )
    counts: defaultdict[str, int] = defaultdict(int)
    for row in sizes:
        counts[row.status] += row.count
    async with User.system().owner as session:
        projection_pending = (
            await session.exec(select(Chunk.id.count()).where(Chunk.processed_at.is_(None)))
        ).one()
    return TasksStatus(
        pending=counts[QueueStatus.queued],
        running=counts[QueueStatus.picked],
        failed=counts[QueueStatus.failed],
        last_success=TasksStatus.stamp(last_success),
        oldest_queued=TasksStatus.stamp(oldest_queued),
        projection_pending=projection_pending,
    )
