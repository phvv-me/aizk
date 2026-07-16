import asyncio
from functools import partial
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest
from bg_doubles import RecordingPg
from pgqueuer import PgQueuer
from pgqueuer.errors import DuplicateJobError
from pgqueuer.executors import DatabaseRetryEntrypointExecutor
from pgqueuer.models import Job

import aizk.common.queue.client as queue_client
from aizk.common.queue import Queue, QueueJob, QueuePayload


class Payload(QueuePayload):
    """Test queue payload."""

    value: int


class ExampleJob(QueueJob[Payload]):
    """Test job recording typed values."""

    entrypoint: ClassVar[str] = "example"
    payload_type: ClassVar[type[Payload]] = Payload

    def __init__(self) -> None:
        self.values: list[int] = []

    async def handle(self, payload: Payload) -> None:
        self.values.append(payload.value)


def test_job_consumption_validates_payloads_and_binds_the_uniform_recovery_policy() -> None:
    job = ExampleJob()
    pg = RecordingPg()

    job.bind(cast(PgQueuer, pg))
    asyncio.run(pg.entrypoints[job.entrypoint](SimpleNamespace(payload=Payload(value=3).encode())))

    assert job.values == [3]
    assert job.priority == job.concurrency_limit == 0
    assert job.max_attempts == 5
    assert pg.failure_policies == {"example": "hold"}
    factory = pg.executor_factories["example"]
    assert isinstance(factory, partial)
    assert factory.func is DatabaseRetryEntrypointExecutor
    assert factory.keywords == {"max_attempts": 5}
    with pytest.raises(AssertionError):
        asyncio.run(job.consume(cast(Job, SimpleNamespace(payload=None))))


def test_queue_owns_connection_worker_enqueue_and_duplicate_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bytes, int, str]] = []
    closed: list[bool] = []
    requeued: list[list[int]] = []
    worker = RecordingPg()

    class Connection:
        async def close(self) -> None:
            closed.append(True)

    query_drivers: list[Connection] = []

    class Queries:
        async def enqueue(
            self,
            entrypoint: str,
            payload: bytes,
            priority: int,
            dedupe_key: str,
        ) -> None:
            if calls:
                raise DuplicateJobError([dedupe_key])
            calls.append((entrypoint, payload, priority, dedupe_key))

        async def list_failed_jobs(self, limit: int) -> list[SimpleNamespace]:
            assert limit in (2, 3)
            return [
                SimpleNamespace(id=1, entrypoint="example"),
                SimpleNamespace(id=2, entrypoint="other"),
            ]

        async def requeue_jobs(self, ids: list[int]) -> None:
            requeued.append(ids)

    def build_queries(driver: Connection) -> Queries:
        query_drivers.append(driver)
        return Queries()

    connection = Connection()

    async def connect(dsn: str) -> Connection:
        assert dsn == "postgresql://queue"
        return connection

    monkeypatch.setattr(queue_client, "asyncpg", SimpleNamespace(connect=connect))
    monkeypatch.setattr(queue_client, "AsyncpgDriver", lambda opened: opened)
    monkeypatch.setattr(queue_client, "Queries", build_queries)
    monkeypatch.setattr(
        queue_client,
        "PgQueuer",
        SimpleNamespace(from_asyncpg_connection=lambda opened: worker),
    )

    async def exercise() -> tuple[bool, bool, int, int]:
        async with Queue(dsn="postgresql://queue") as queue:
            assert queue.worker() is worker
            job = ExampleJob()
            first = await job.enqueue(queue, Payload(value=7), "same")
            second = await job.enqueue(queue, Payload(value=8), "same")
            matched = await queue.requeue_failed(job, 3)
            unmatched = await queue.requeue_failed(
                cast(QueueJob[Payload], SimpleNamespace(entrypoint="missing")), 2
            )
            return first, second, matched, unmatched

    assert asyncio.run(exercise()) == (True, False, 1, 0)
    assert calls == [("example", Payload(value=7).encode(), 0, "same")]
    assert requeued == [[1]]
    assert query_drivers == [connection]
    assert closed == [True]


def test_queue_rejects_operations_before_its_connection_is_open() -> None:
    queue = Queue(dsn="postgresql://queue")

    with pytest.raises(RuntimeError, match="not open"):
        queue.worker()
