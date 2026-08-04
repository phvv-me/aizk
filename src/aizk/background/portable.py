import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Protocol

from croniter import croniter
from loguru import logger
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import select

from ..config import settings
from ..store.identity import User
from ..store.models.tables.queue import QueueEvent, QueueSchedule, QueueTask
from .enum import QueueStatus

type ScheduledCallback = Callable[[], Awaitable[None]]


class PortableJob(Protocol):
    """Typed job surface consumed by the portable worker."""

    entrypoint: ClassVar[str]
    concurrency_limit: ClassVar[int]

    async def handle_encoded(self, payload: bytes) -> None: ...


class PortableWorker:
    """Poll durable CockroachDB jobs and coordinate cron callbacks without PostgreSQL hooks."""

    def __init__(
        self,
        jobs: Iterable[PortableJob],
        schedules: Mapping[str, tuple[str, ScheduledCallback]],
        batch_size: int,
    ) -> None:
        self.jobs = {job.entrypoint: job for job in jobs}
        self.schedules = dict(schedules)
        self.batch_size = batch_size
        self.semaphores = {
            name: asyncio.Semaphore(job.concurrency_limit or batch_size)
            for name, job in self.jobs.items()
        }

    @staticmethod
    def next_run(expression: str, after: datetime) -> datetime:
        """Return the next UTC occurrence for one cron expression."""
        return croniter(expression, after).get_next(datetime)

    async def install_schedules(self) -> None:
        """Upsert enabled schedules while retaining an unchanged durable cursor."""
        now = datetime.now(UTC)
        async with User.system().owner as session:
            for name, (expression, _) in self.schedules.items():
                statement = insert(QueueSchedule).values(
                    name=name,
                    expression=expression,
                    next_run=self.next_run(expression, now),
                )
                await session.exec(
                    statement.on_conflict_do_update(
                        index_elements=[QueueSchedule.name],
                        set_={
                            "expression": expression,
                            "next_run": QueueSchedule.next_run,
                        },
                    )
                )

    async def fire_schedules(self) -> int:
        """Claim and invoke every currently due schedule once."""
        fired = 0
        while True:
            now = datetime.now(UTC)
            async with User.system().owner as session:
                schedule = (
                    await session.exec(
                        select(QueueSchedule)
                        .where(
                            QueueSchedule.name.in_(self.schedules),
                            QueueSchedule.next_run <= now,
                        )
                        .order_by(QueueSchedule.next_run, QueueSchedule.name)
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                ).first()
                if schedule is None:
                    return fired
                expression, callback = self.schedules[schedule.name]
                schedule.next_run = self.next_run(expression, now)
            try:
                await callback()
            except BaseException as error:
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                logger.exception("portable schedule {} failed", schedule.name)
            fired += 1

    async def claim(self) -> list[QueueTask]:
        """Lease one ordered batch by changing each row to picked in a short transaction."""
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=settings.queue_lease_seconds)
        async with User.system().owner as session:
            stale = list(
                await session.exec(
                    select(QueueTask)
                    .where(
                        QueueTask.status == QueueStatus.picked.value,
                        QueueTask.heartbeat_at < stale_before,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for task in stale:
                task.status = QueueStatus.queued.value
                task.available_at = now
                task.heartbeat_at = None
                task.error_type = "WorkerLeaseExpired"
                task.error_message = "worker heartbeat lease expired"
            tasks = list(
                await session.exec(
                    select(QueueTask)
                    .where(
                        QueueTask.status == QueueStatus.queued.value,
                        QueueTask.available_at <= now,
                    )
                    .order_by(QueueTask.priority.desc(), QueueTask.created_at, QueueTask.id)
                    .limit(self.batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            for task in tasks:
                task.status = QueueStatus.picked.value
                task.heartbeat_at = now
                task.attempts += 1
            return tasks

    async def heartbeat(self, task: QueueTask) -> None:
        """Refresh one running task until its handler finishes or the heartbeat is cancelled."""
        while True:
            await asyncio.sleep(settings.queue_heartbeat_seconds)
            async with User.system().owner as session:
                stored = await session.get(QueueTask, task.id)
                if stored is None or stored.status != QueueStatus.picked.value:
                    return
                stored.heartbeat_at = datetime.now(UTC)

    async def finish(self, task: QueueTask) -> None:
        """Mark one handled task successful and append its execution event."""
        async with User.system().owner as session:
            stored = await session.get(QueueTask, task.id)
            if stored is None:
                return
            stored.status = QueueStatus.successful.value
            stored.heartbeat_at = None
            stored.error_type = None
            stored.error_message = None
            session.add(
                QueueEvent(
                    task_id=stored.id,
                    entrypoint=stored.entrypoint,
                    status=QueueStatus.successful.value,
                    attempts=stored.attempts,
                )
            )

    async def fail(self, task: QueueTask, error: BaseException) -> None:
        """Retry one failed task with bounded backoff or retain it as terminal."""
        async with User.system().owner as session:
            stored = await session.get(QueueTask, task.id)
            if stored is None:
                return
            terminal = stored.attempts >= stored.max_attempts
            stored.status = QueueStatus.failed.value if terminal else QueueStatus.queued.value
            stored.available_at = datetime.now(UTC) + timedelta(
                seconds=settings.queue_retry_base_seconds * 2 ** max(stored.attempts - 1, 0)
            )
            stored.heartbeat_at = None
            stored.error_type = type(error).__name__
            stored.error_message = str(error)
            session.add(
                QueueEvent(
                    task_id=stored.id,
                    entrypoint=stored.entrypoint,
                    status=QueueStatus.failed.value,
                    attempts=stored.attempts,
                    error_type=stored.error_type,
                    error_message=stored.error_message,
                )
            )

    async def execute(self, task: QueueTask) -> None:
        """Decode and execute one claimed task through its registered typed job."""
        job = self.jobs.get(task.entrypoint)
        if job is None:
            await self.fail(task, LookupError(f"unregistered job {task.entrypoint}"))
            return
        heartbeat = asyncio.create_task(self.heartbeat(task))
        try:
            async with self.semaphores[task.entrypoint]:
                await job.handle_encoded(task.payload)
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            logger.exception("portable job {} failed", task.entrypoint)
            await self.fail(task, error)
            return
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        await self.finish(task)

    async def run_once(self) -> int:
        """Fire due schedules and execute one queue batch, returning handled job count."""
        await self.fire_schedules()
        tasks = await self.claim()
        await asyncio.gather(*(self.execute(task) for task in tasks))
        return len(tasks)

    async def run(self) -> None:
        """Poll schedules and tasks until the service is cancelled."""
        await self.install_schedules()
        logger.info("portable CockroachDB worker polling the durable queue")
        while True:
            handled = await self.run_once()
            if handled == 0:
                await asyncio.sleep(settings.queue_poll_seconds)
