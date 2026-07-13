import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Self

import pytest
from pgqueuer.errors import DuplicateJobError

import aizk.background.queue as queue_mod

type AsyncCallback = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class FakeUser:
    id: uuid.UUID


@dataclass
class EnqueueCall:
    entrypoint: str
    payload: bytes
    dedupe_key: str | None


@dataclass
class RecordingQueue:
    enqueues: list[EnqueueCall] = field(default_factory=list)
    opened: int = 0
    closed: int = 0

    async def enqueue(
        self, entrypoint: str, payload: bytes, dedupe_key: str | None = None
    ) -> None:
        if dedupe_key is not None and any(call.dedupe_key == dedupe_key for call in self.enqueues):
            raise DuplicateJobError([dedupe_key])
        self.enqueues.append(EnqueueCall(entrypoint, payload, dedupe_key))


def patch_queue_seam(monkeypatch: pytest.MonkeyPatch, module: object) -> RecordingQueue:
    recorder = RecordingQueue()

    async def fake_connect(dsn: str) -> SimpleNamespace:
        recorder.opened += 1

        async def close() -> None:
            recorder.closed += 1

        return SimpleNamespace(close=close)

    monkeypatch.setattr(queue_mod, "asyncpg", SimpleNamespace(connect=fake_connect))
    monkeypatch.setattr(module, "AsyncpgDriver", lambda connection: connection)
    monkeypatch.setattr(module, "Queries", lambda driver: recorder)
    return recorder


@dataclass
class RecordingPg:
    entrypoints: dict[str, Callable[..., Awaitable[None]]] = field(default_factory=dict)
    concurrency_limits: dict[str, int | None] = field(default_factory=dict)
    schedules: list[tuple[str, str, Callable[..., Awaitable[None]]]] = field(default_factory=list)
    runs: list[int] = field(default_factory=list)

    @classmethod
    def from_asyncpg_connection(cls, connection: object) -> Self:
        return cls()

    def entrypoint[T: AsyncCallback](
        self, name: str, *, concurrency_limit: int = 0
    ) -> Callable[[T], T]:
        self.concurrency_limits[name] = concurrency_limit

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
def queue_seam(monkeypatch: pytest.MonkeyPatch) -> Callable[[object], RecordingQueue]:
    def install(module: object) -> RecordingQueue:
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
