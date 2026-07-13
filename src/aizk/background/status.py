from patos import FrozenModel
from sqlalchemy import DateTime, String, column, func, select, table
from sqlalchemy.dialects.postgresql import ENUM

from ..store.identity import User
from .queue import EXTRACT_ENTRYPOINT


class TasksStatus(FrozenModel):
    """The autonomous engine's operational snapshot, the admin status read."""

    pending: int
    running: int
    failed: int
    last_run: str | None
    lag: int


async def tasks_overview() -> TasksStatus:
    """Read the queue tables for the engine's pending, running, failed, last-run, and lag
    counts."""
    status = ENUM(
        "queued",
        "picked",
        "successful",
        "exception",
        "canceled",
        "deleted",
        name="pgqueuer_status",
        create_type=False,
    )
    jobs = table(
        "pgqueuer",
        column("status", status),
        column("entrypoint", String),
    )
    logs = table(
        "pgqueuer_log",
        column("status", status),
        column("created", DateTime(timezone=True)),
    )
    count = func.count()
    statement = select(
        select(count).select_from(jobs).where(jobs.c.status == "queued").scalar_subquery(),
        select(count).select_from(jobs).where(jobs.c.status == "picked").scalar_subquery(),
        select(count).select_from(logs).where(logs.c.status == "exception").scalar_subquery(),
        select(func.max(logs.c.created)).scalar_subquery(),
        select(count)
        .select_from(jobs)
        .where(jobs.c.status == "queued", jobs.c.entrypoint == EXTRACT_ENTRYPOINT)
        .scalar_subquery(),
    )
    async with User.system() as db:
        pending, running, failed, last_run, lag = (await db.exec(statement)).one()
    return TasksStatus(
        pending=pending or 0,
        running=running or 0,
        failed=failed or 0,
        last_run=last_run.isoformat() if last_run is not None else None,
        lag=lag or 0,
    )
