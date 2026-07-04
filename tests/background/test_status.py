from datetime import UTC, datetime

import dbutil

from aizk.background.queue import EXTRACT_ENTRYPOINT
from aizk.background.status import tasks_overview

# the queue columns pgqueuer marks NOT NULL that a seeded row must carry, timestamps fixed since
# the status read never inspects them beyond the log's `created`.
_STAMP = datetime(2020, 1, 1, tzinfo=UTC)


async def clear_queue() -> None:
    """Wipe the pgqueuer tables so each status read starts from an empty, isolated queue."""
    await dbutil.admin_exec("TRUNCATE pgqueuer, pgqueuer_log RESTART IDENTITY")


async def seed_job(status: str, entrypoint: str) -> None:
    """Insert one live queue row in the given status under the given entrypoint, as the owner."""
    await dbutil.admin_exec(
        "INSERT INTO pgqueuer "
        "(priority, created, updated, heartbeat, execute_after, status, entrypoint) "
        "VALUES (0, :t, :t, :t, :t, CAST(:s AS pgqueuer_status), :e)",
        {"t": _STAMP, "s": status, "e": entrypoint},
    )


async def seed_log(status: str, created: datetime) -> None:
    """Insert one completed-job log row in the given status stamped at `created`, as the owner."""
    await dbutil.admin_exec(
        "INSERT INTO pgqueuer_log (created, job_id, status, priority, entrypoint) "
        "VALUES (:c, 1, CAST(:s AS pgqueuer_status), 0, 'e')",
        {"c": created, "s": status},
    )


def test_tasks_overview_reads_the_live_queue_and_log_counts(migrated_db: None) -> None:
    """The overview sums queued as pending, picked as running, exception logs as failed, extraction
    backlog as lag, and reports the newest log `created` as the ISO last-run timestamp.

    Lag counts only queued extraction jobs, so a queued job on another entrypoint lifts pending but
    not lag, the embed-to-extract backlog the admin watches distinct from the whole queue depth.
    """
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
    """A queue that has never run reports zero counts and a null last-run, not raw None counts."""
    dbutil.run(clear_queue())
    status = dbutil.run(tasks_overview())

    assert (status.pending, status.running, status.failed, status.lag) == (0, 0, 0, 0)
    assert status.last_run is None
