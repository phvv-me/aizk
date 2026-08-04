import asyncio
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import dbutil
import pytest
from sqlmodel import delete, select
from sqlmodel.sql.expression import SelectOfScalar

import aizk.background.portable as portable_mod
from aizk.background.enum import QueueStatus
from aizk.background.portable import PortableWorker
from aizk.background.queue import Queue, QueueJob, QueuePayload, install_queue_schema
from aizk.config import DatabaseBackend, settings
from aizk.store.identity import User
from aizk.store.locking import acquire_locks
from aizk.store.models.tables.coordination_lock import CoordinationLock
from aizk.store.models.tables.queue import QueueEvent, QueueSchedule, QueueTask


class Payload(QueuePayload):
    """Portable queue test payload."""

    value: int


class JobContract(QueueJob[Payload]):
    """Queue declaration used to persist portable test jobs."""

    entrypoint: ClassVar[str] = "portable_test"
    payload_type: ClassVar[type[Payload]] = Payload
    priority: ClassVar[int] = 4
    max_attempts: ClassVar[int] = 2

    async def handle(self, payload: Payload) -> None:
        del payload


class RecordingJob:
    """Portable handler that records payloads or raises a configured error."""

    entrypoint: ClassVar[str] = JobContract.entrypoint
    concurrency_limit: ClassVar[int] = 1

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.payloads: list[bytes] = []

    async def handle_encoded(self, payload: bytes) -> None:
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error


async def clear_portable_queue() -> None:
    """Clear migration-owned portable queue tables in dependency order."""
    async with User.system().owner as session:
        await session.exec(delete(QueueEvent))
        await session.exec(delete(QueueTask))
        await session.exec(delete(QueueSchedule))
        await session.exec(delete(CoordinationLock))


def select_task(dedupe_key: str) -> SelectOfScalar[QueueTask]:
    return select(QueueTask).where(QueueTask.dedupe_key == dedupe_key)


def test_portable_queue_runs_successfully_and_reports_backend_neutral_state(
    migrated_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "database_backend", DatabaseBackend.cockroachdb)

    async def run() -> None:
        await clear_portable_queue()
        job = RecordingJob()
        worker = PortableWorker([job], {}, batch_size=4)
        payload = Payload(value=7)
        await JobContract().handle_encoded(payload.encode())
        await install_queue_schema()
        async with User.system().owner as session:
            await acquire_locks(session, ("second", "first", "second"))
            lock_keys = list(
                await session.exec(select(CoordinationLock.key).order_by(CoordinationLock.key))
            )
            assert lock_keys == ["first", "second"]
        async with Queue(dsn=settings.asyncpg_dsn) as queue:
            with pytest.raises(RuntimeError, match="not open"):
                _ = queue.connection
            with pytest.raises(RuntimeError, match="PortableWorker"):
                queue.worker()
            assert await queue.enqueue(JobContract, payload, "success") is True
            assert await queue.enqueue(JobContract, payload, "success") is False
            assert await queue.active_payloads(JobContract.entrypoint) == (payload.encode(),)
            pending = await queue.snapshot()
            assert (pending.pending, pending.running, pending.failed) == (1, 0, 0)
            assert pending.oldest_queued is not None

        assert await worker.run_once() == 1
        assert job.payloads == [payload.encode()]
        async with Queue(dsn=settings.asyncpg_dsn) as queue:
            complete = await queue.snapshot()
            assert (complete.pending, complete.running, complete.failed) == (0, 0, 0)
            assert complete.last_success is not None
            assert complete.oldest_queued is None
            assert await queue.active_payloads(JobContract.entrypoint) == ()
            assert await queue.enqueue(JobContract, payload, "success") is True

    dbutil.run(run())


def test_portable_worker_retries_then_retains_and_requeues_failures(
    migrated_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "database_backend", DatabaseBackend.cockroachdb)
    monkeypatch.setattr(settings, "queue_retry_base_seconds", 0.001)

    async def run() -> None:
        await clear_portable_queue()
        payload = Payload(value=9)
        async with Queue(dsn=settings.asyncpg_dsn) as queue:
            assert await queue.enqueue(JobContract, payload, "failure")
        worker = PortableWorker([RecordingJob(ValueError("private payload"))], {}, batch_size=1)

        assert await worker.run_once() == 1
        async with User.system().owner as session:
            retry = (await session.exec(select_task("failure"))).one()
            assert retry.status == QueueStatus.queued.value
            assert retry.attempts == 1
            assert retry.error_type == "ValueError"
            retry.available_at = datetime.now(UTC) - timedelta(seconds=1)

        assert await worker.run_once() == 1
        async with Queue(dsn=settings.asyncpg_dsn) as queue:
            retained = await queue.snapshot()
            assert retained.failed == 1
            assert await queue.requeue_failed(JobContract, max_cycles=2) == 0
            assert await queue.requeue_failed(JobContract) == 1
            assert await queue.active_payloads(JobContract.entrypoint) == (payload.encode(),)
        async with User.system().owner as session:
            events = list(await session.exec(select(QueueEvent).order_by(QueueEvent.created_at)))
            assert [event.status for event in events] == ["failed", "failed"]
            assert all(event.error_message == "private payload" for event in events)

    dbutil.run(run())


def test_portable_worker_recovers_expired_leases_and_handles_missing_jobs(
    migrated_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "database_backend", DatabaseBackend.cockroachdb)
    monkeypatch.setattr(settings, "queue_lease_seconds", 10)

    async def run() -> None:
        await clear_portable_queue()
        now = datetime.now(UTC)
        async with User.system().owner as session:
            stale = QueueTask(
                entrypoint="missing",
                payload=b"{}",
                status=QueueStatus.picked.value,
                attempts=0,
                max_attempts=1,
                available_at=now,
                heartbeat_at=now - timedelta(minutes=1),
            )
            session.add(stale)
            await session.flush()
            stale_id = stale.id

        worker = PortableWorker([], {}, batch_size=1)
        assert await worker.run_once() == 1
        async with User.system().owner as session:
            retained = await session.get(QueueTask, stale_id)
            assert retained is not None
            assert retained.status == QueueStatus.failed.value
            assert retained.attempts == 1
            assert retained.error_type == "LookupError"

        absent = QueueTask(
            entrypoint="absent",
            payload=b"{}",
            available_at=now,
        )
        await worker.finish(absent)
        await worker.fail(absent, ValueError("ignored"))

    dbutil.run(run())


def test_portable_heartbeat_exits_for_missing_or_completed_tasks(
    migrated_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "database_backend", DatabaseBackend.cockroachdb)
    monkeypatch.setattr(settings, "queue_heartbeat_seconds", 0.001)

    async def run() -> None:
        await clear_portable_queue()
        worker = PortableWorker([], {}, batch_size=1)
        now = datetime.now(UTC)
        missing = QueueTask(entrypoint="missing", payload=b"{}", available_at=now)
        await worker.heartbeat(missing)
        async with User.system().owner as session:
            picked = QueueTask(
                entrypoint="picked",
                payload=b"{}",
                status=QueueStatus.picked.value,
                available_at=now,
                heartbeat_at=now - timedelta(minutes=1),
            )
            completed = QueueTask(
                entrypoint="complete",
                payload=b"{}",
                status=QueueStatus.successful.value,
                available_at=now,
            )
            session.add_all((picked, completed))
            await session.flush()
        await worker.heartbeat(completed)

        sleeps = 0

        async def one_cycle(delay: float) -> None:
            nonlocal sleeps
            del delay
            sleeps += 1
            if sleeps > 1:
                raise asyncio.CancelledError

        monkeypatch.setattr(portable_mod.asyncio, "sleep", one_cycle)
        with pytest.raises(asyncio.CancelledError):
            await worker.heartbeat(picked)
        async with User.system().owner as session:
            refreshed = await session.get(QueueTask, picked.id)
            assert refreshed is not None
            assert refreshed.heartbeat_at is not None
            assert refreshed.heartbeat_at > now

    dbutil.run(run())


def test_portable_schedules_keep_durable_cursors_and_isolate_callback_failures(
    migrated_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "database_backend", DatabaseBackend.cockroachdb)

    async def run() -> None:
        await clear_portable_queue()
        calls: list[str] = []

        async def tick() -> None:
            calls.append("tick")

        worker = PortableWorker([], {"tick": ("* * * * *", tick)}, batch_size=1)
        after = datetime(2026, 7, 23, tzinfo=UTC)
        assert worker.next_run("* * * * *", after) > after
        await worker.install_schedules()
        async with User.system().owner as session:
            schedule = await session.get(QueueSchedule, "tick")
            assert schedule is not None
            cursor = schedule.next_run
        await worker.install_schedules()
        async with User.system().owner as session:
            schedule = await session.get(QueueSchedule, "tick")
            assert schedule is not None
            assert schedule.next_run == cursor
            schedule.next_run = datetime.now(UTC) - timedelta(seconds=1)
        assert await worker.fire_schedules() == 1
        assert calls == ["tick"]
        assert await worker.fire_schedules() == 0

        async def broken() -> None:
            raise ValueError("schedule failed")

        worker.schedules["tick"] = ("* * * * *", broken)
        async with User.system().owner as session:
            schedule = await session.get(QueueSchedule, "tick")
            assert schedule is not None
            schedule.next_run = datetime.now(UTC) - timedelta(seconds=1)
        assert await worker.fire_schedules() == 1

        async def cancelled() -> None:
            raise asyncio.CancelledError

        worker.schedules["tick"] = ("* * * * *", cancelled)
        async with User.system().owner as session:
            schedule = await session.get(QueueSchedule, "tick")
            assert schedule is not None
            schedule.next_run = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(asyncio.CancelledError):
            await worker.fire_schedules()

    dbutil.run(run())


def test_portable_execute_and_polling_propagate_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cancelled(payload: bytes) -> None:
        del payload
        raise asyncio.CancelledError

    job = RecordingJob()
    job.handle_encoded = cancelled
    worker = PortableWorker([job], {}, batch_size=1)
    task = QueueTask(
        entrypoint=job.entrypoint,
        payload=b"{}",
        available_at=datetime.now(UTC),
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.execute(task))

    installed: list[None] = []

    async def install() -> None:
        installed.append(None)

    handled = iter((1, 0))

    async def empty() -> int:
        return next(handled)

    async def stop(delay: float) -> None:
        del delay
        raise asyncio.CancelledError

    worker.install_schedules = install
    worker.run_once = empty
    monkeypatch.setattr(portable_mod.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.run())
    assert installed == [None]
