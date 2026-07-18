from collections import defaultdict

from patos import FrozenModel

from ..config import settings
from .enum import QueueStatus
from .jobs.projection import ChunkProjectionJob
from .queue import Queue


class TasksStatus(FrozenModel):
    """The autonomous engine's operational snapshot, the admin status read."""

    pending: int
    running: int
    failed: int
    last_run: str | None
    lag: int


async def tasks_overview() -> TasksStatus:
    """Read the operational snapshot through PgQueuer's own query facade."""
    async with Queue(dsn=settings.asyncpg_dsn) as queue:
        sizes = await queue.queries.queue_size()
        logs = await queue.queries.queue_log()
    counts: defaultdict[str, int] = defaultdict(int)
    lag = 0
    for row in sizes:
        counts[row.status] += row.count
        if row.status == QueueStatus.queued and row.entrypoint == ChunkProjectionJob.entrypoint:
            lag += row.count
    last_run = max((row.created for row in logs), default=None)
    return TasksStatus(
        pending=counts[QueueStatus.queued],
        running=counts[QueueStatus.picked],
        failed=counts[QueueStatus.failed],
        last_run=last_run.isoformat() if last_run is not None else None,
        lag=lag,
    )
