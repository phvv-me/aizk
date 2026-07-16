from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import ModuleType, TracebackType

import pytest
from pgqueuer.executors import AbstractEntrypointExecutor, EntrypointExecutorParameters
from pgqueuer.types import OnFailure
from pydantic import UUID5, UUID7

from aizk.common.queue import QueueJob, QueuePayload

type AsyncCallback = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class FakeUser:
    id: UUID5 | UUID7


@dataclass
class EnqueueCall:
    entrypoint: str
    payload: bytes
    dedupe_key: str | None
    priority: int


@dataclass
class RecordingQueue:
    enqueues: list[EnqueueCall] = field(default_factory=list)
    failed_requeues: list[tuple[str, int]] = field(default_factory=list)
    opened: int = 0
    closed: int = 0
    worker_instance: RecordingPg | None = None

    async def __aenter__(self) -> RecordingQueue:
        self.opened += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed += 1

    async def enqueue[PayloadT: QueuePayload](
        self,
        job: QueueJob[PayloadT],
        payload: PayloadT,
        dedupe_key: str,
    ) -> bool:
        if dedupe_key is not None and any(call.dedupe_key == dedupe_key for call in self.enqueues):
            return False
        self.enqueues.append(
            EnqueueCall(job.entrypoint, payload.encode(), dedupe_key, job.priority)
        )
        return True

    async def requeue_failed[PayloadT: QueuePayload](
        self, job: QueueJob[PayloadT], limit: int = 100
    ) -> int:
        self.failed_requeues.append((job.entrypoint, limit))
        return 4

    def worker(self) -> RecordingPg:
        assert self.worker_instance is not None
        return self.worker_instance


def patch_queue_seam(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> RecordingQueue:
    recorder = RecordingQueue()

    def fake_queue(*, dsn: str) -> RecordingQueue:
        del dsn
        return recorder

    monkeypatch.setattr(module, "Queue", fake_queue)
    return recorder


@dataclass
class RecordingPg:
    entrypoints: dict[str, Callable[..., Awaitable[None]]] = field(default_factory=dict)
    concurrency_limits: dict[str, int | None] = field(default_factory=dict)
    failure_policies: dict[str, OnFailure] = field(default_factory=dict)
    executor_factories: dict[
        str,
        Callable[[EntrypointExecutorParameters], AbstractEntrypointExecutor] | None,
    ] = field(default_factory=dict)
    schedules: list[tuple[str, str, Callable[..., Awaitable[None]]]] = field(default_factory=list)
    runs: list[int] = field(default_factory=list)

    def entrypoint[T: AsyncCallback](
        self,
        name: str,
        *,
        concurrency_limit: int = 0,
        accepts_context: bool | None = None,
        on_failure: OnFailure = "delete",
        executor_factory: Callable[[EntrypointExecutorParameters], AbstractEntrypointExecutor]
        | None = None,
    ) -> Callable[[T], T]:
        del accepts_context
        self.concurrency_limits[name] = concurrency_limit
        self.failure_policies[name] = on_failure
        self.executor_factories[name] = executor_factory

        def register(body: T) -> T:
            self.entrypoints[name] = body
            return body

        return register

    def schedule[T: AsyncCallback](self, entrypoint: str, expression: str) -> Callable[[T], T]:
        def register(body: T) -> T:
            self.schedules.append((entrypoint, expression, body))
            return body

        return register

    async def run(self, batch_size: int = 10, max_concurrent_tasks: int | None = None) -> None:
        self.runs.append(batch_size)


@dataclass
class FakeJob:
    payload: bytes


@pytest.fixture
def queue_seam(monkeypatch: pytest.MonkeyPatch) -> Callable[[ModuleType], RecordingQueue]:
    def install(module: ModuleType) -> RecordingQueue:
        return patch_queue_seam(monkeypatch, module)

    return install


@pytest.fixture
def pg_factory() -> type[RecordingPg]:
    return RecordingPg


@pytest.fixture
def job_factory() -> type[FakeJob]:
    return FakeJob


@pytest.fixture
def user_factory() -> type[FakeUser]:
    return FakeUser
