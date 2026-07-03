import asyncpg
from patos import FrozenModel

from ..config import settings
from .queue import EXTRACT_ENTRYPOINT


class TasksStatus(FrozenModel):
    """The autonomous engine's operational snapshot, the admin status read.

    pending: jobs waiting in the queue.
    running: jobs a worker has picked and is processing.
    failed: jobs the log records as having raised.
    last_run: ISO timestamp of the most recent logged job, null when none has run.
    lag: chunks queued for extraction but not yet processed, the embed-to-extract backlog.
    """

    pending: int
    running: int
    failed: int
    last_run: str | None
    lag: int


async def tasks_overview() -> TasksStatus:
    """Read the queue tables for the engine's pending, running, failed, last-run, and lag counts.

    Opens a short asyncpg connection on the app DSN, which the install grants read on the queue
    tables, and reads the live queue for what is waiting and in flight, the log for what failed and
    when the last job ran, and the extraction backlog as the embed-to-extract lag.
    """
    connection = await asyncpg.connect(settings.asyncpg_dsn)
    try:
        pending = await connection.fetchval(
            "SELECT count(*) FROM pgqueuer WHERE status = 'queued'"
        )
        running = await connection.fetchval(
            "SELECT count(*) FROM pgqueuer WHERE status = 'picked'"
        )
        failed = await connection.fetchval(
            "SELECT count(*) FROM pgqueuer_log WHERE status = 'exception'"
        )
        last_run = await connection.fetchval("SELECT max(created) FROM pgqueuer_log")
        lag = await connection.fetchval(
            "SELECT count(*) FROM pgqueuer WHERE status = 'queued' AND entrypoint = $1",
            EXTRACT_ENTRYPOINT,
        )
    finally:
        await connection.close()
    return TasksStatus(
        pending=pending or 0,
        running=running or 0,
        failed=failed or 0,
        last_run=last_run.isoformat() if last_run else None,
        lag=lag or 0,
    )
