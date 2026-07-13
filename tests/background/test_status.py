from datetime import UTC, datetime

import dbutil

from aizk.background.queue import EXTRACT_ENTRYPOINT
from aizk.background.status import tasks_overview

# Required pgqueuer fields not read by the status query
_STAMP = datetime(2020, 1, 1, tzinfo=UTC)


async def clear_queue() -> None:
    await dbutil.admin_exec("TRUNCATE pgqueuer, pgqueuer_log RESTART IDENTITY")


async def seed_job(status: str, entrypoint: str) -> None:
    await dbutil.admin_exec(
        "INSERT INTO pgqueuer "
        "(priority, created, updated, heartbeat, execute_after, status, entrypoint) "
        "VALUES (0, :t, :t, :t, :t, CAST(:s AS pgqueuer_status), :e)",
        {"t": _STAMP, "s": status, "e": entrypoint},
    )


async def seed_log(status: str, created: datetime) -> None:
    await dbutil.admin_exec(
        "INSERT INTO pgqueuer_log (created, job_id, status, priority, entrypoint) "
        "VALUES (:c, 1, CAST(:s AS pgqueuer_status), 0, 'e')",
        {"c": created, "s": status},
    )


def test_tasks_overview_reads_the_live_queue_and_log_counts(migrated_db: None) -> None:
    last = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)

    async def body() -> None:
        await clear_queue()
        await seed_job("queued", EXTRACT_ENTRYPOINT)
        await seed_job("queued", EXTRACT_ENTRYPOINT)
        await seed_job("queued", "aizk_task_decay")
        await seed_job("picked", EXTRACT_ENTRYPOINT)
        await seed_log("exception", datetime(2026, 1, 1, tzinfo=UTC))
        await seed_log("exception", last)
        await seed_log("successful", datetime(2025, 1, 1, tzinfo=UTC))

    dbutil.run(body())
    status = dbutil.run(tasks_overview())

    assert (status.pending, status.running, status.failed, status.lag) == (3, 1, 2, 2)
    assert status.last_run == last.isoformat()


def test_tasks_overview_defaults_an_empty_queue_to_zeros_and_null_last_run(
    migrated_db: None,
) -> None:
    dbutil.run(clear_queue())
    status = dbutil.run(tasks_overview())

    assert (status.pending, status.running, status.failed, status.lag) == (0, 0, 0, 0)
    assert status.last_run is None
