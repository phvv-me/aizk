from datetime import UTC, datetime

import dbutil
import pytest

from aizk.background.jobs.projection import ChunkProjectionJob
from aizk.background.status import tasks_overview

# Required pgqueuer fields not read by the status query
_STAMP = datetime(2020, 1, 1, tzinfo=UTC)


async def clear_queue() -> None:
    await dbutil.admin_exec(
        "TRUNCATE pgqueuer, pgqueuer_log, pgqueuer_statistics RESTART IDENTITY"
    )


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


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
def test_tasks_overview_summarizes_empty_and_populated_queues(
    migrated_db: None, populated: bool
) -> None:
    last = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)

    async def body() -> None:
        await clear_queue()
        if not populated:
            return
        await seed_job("queued", ChunkProjectionJob().entrypoint)
        await seed_job("queued", ChunkProjectionJob().entrypoint)
        await seed_job("queued", "aizk_task_decay")
        await seed_job("picked", ChunkProjectionJob().entrypoint)
        await seed_job("failed", ChunkProjectionJob().entrypoint)
        await seed_job("failed", "aizk_task_decay")
        await seed_log("exception", datetime(2026, 1, 1, tzinfo=UTC))
        await seed_log("exception", last)
        await seed_log("successful", datetime(2025, 1, 1, tzinfo=UTC))

    dbutil.run(body())
    status = dbutil.run(tasks_overview())

    expected_counts = (3, 1, 2, 2) if populated else (0, 0, 0, 0)
    assert (status.pending, status.running, status.failed, status.lag) == expected_counts
    assert status.last_run == (last.isoformat() if populated else None)
