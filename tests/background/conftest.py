import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Self

import pytest
from pgqueuer.errors import DuplicateJobError

import aizk.background.queue as queue_mod
from aizk.config import Settings


@dataclass(frozen=True)
class FakePrincipal:
    """The one attribute the fan-out reads off a roster entry, the principal id.

    id: identity the fanned-out job runs its pass for.
    """

    id: uuid.UUID


@dataclass
class EnqueueCall:
    """One recorded enqueue, the unit the fan-out and the on-write chain emit.

    entrypoint: queue entrypoint the job targets.
    payload: encoded job body the worker decodes.
    dedupe_key: collapse key a re-run reuses so a job never piles up.
    """

    entrypoint: str
    payload: bytes
    dedupe_key: str | None


@dataclass
class RecordingQueue:
    """A recording stand-in for the pgqueuer `Queries` seam, the only external boundary mocked.

    It captures every enqueue so a test asserts the entrypoint, the decoded payload, and the dedupe
    key the background code chose, and counts the asyncpg connections opened and closed so the
    short-lived-connection contract is checked without a real database.

    enqueues: every enqueue in the order it was issued.
    opened: how many asyncpg connections the code under test opened.
    closed: how many it closed, equal to opened when every path released its connection.
    """

    enqueues: list[EnqueueCall] = field(default_factory=list)
    opened: int = 0
    closed: int = 0

    async def enqueue(
        self, entrypoint: str, payload: bytes, dedupe_key: str | None = None
    ) -> None:
        """Record one enqueue exactly as the production queue would receive it.

        A repeated dedupe key raises `DuplicateJobError` the way the real pgqueuer does, so the
        harmless-re-enqueue contract every enqueue path documents is actually exercised.

        entrypoint: queue entrypoint the job targets.
        payload: encoded job body.
        dedupe_key: collapse key for the job.
        """
        if dedupe_key is not None and any(call.dedupe_key == dedupe_key for call in self.enqueues):
            raise DuplicateJobError(dedupe_key)
        self.enqueues.append(EnqueueCall(entrypoint, payload, dedupe_key))


def patch_queue_seam(monkeypatch: pytest.MonkeyPatch, module: object) -> RecordingQueue:
    """Swap a module's asyncpg-plus-pgqueuer seam for one recording queue, returning the recorder.

    Every connection now opens inside `queue.queue_connection`, so the connect fake always lands on
    the queue module while `Queries` and `AsyncpgDriver` are patched on the module under test,
    exercising the code's real connection lifecycle and enqueue calls against an in-memory double.

    monkeypatch: the pytest patcher.
    module: the background module whose enqueue seam is replaced, schedule or queue.
    """
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
    """A recording stand-in for the PgQueuer application, capturing every registration.

    The worker registers queue entrypoints and cron schedules through decorators and then calls
    `run`; this double keeps the decorated bodies so a test drives them directly and records the
    one `run` call so the worker's start contract is checked without a database.

    entrypoints: decorated queue bodies keyed by entrypoint name.
    schedules: (name, crontab expression, body) for every registered cron.
    runs: batch sizes the worker started the manager with.
    """

    entrypoints: dict[str, Callable[..., Awaitable[None]]] = field(default_factory=dict)
    schedules: list[tuple[str, str, Callable[..., Awaitable[None]]]] = field(default_factory=list)
    runs: list[int] = field(default_factory=list)

    @classmethod
    def from_asyncpg_connection(cls, connection: object) -> Self:
        """Build the application the worker attaches to, ignoring the fake connection.

        connection: the asyncpg connection the real factory would wrap.
        """
        return cls()

    def entrypoint(
        self, name: str
    ) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
        """Record and return a queue entrypoint registration decorator.

        name: entrypoint the decorated body drains.
        """

        def register(body: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
            self.entrypoints[name] = body
            return body

        return register

    def schedule(
        self, name: str, expression: str
    ) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
        """Record and return a cron registration decorator.

        name: cron entrypoint name.
        expression: crontab expression the cron fires on.
        """

        def register(body: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
            self.schedules.append((name, expression, body))
            return body

        return register

    async def run(self, batch_size: int = 10) -> None:
        """Record the worker starting the manager and the scheduler.

        batch_size: maximum jobs the manager dequeues per round.
        """
        self.runs.append(batch_size)


@dataclass
class FakeJob:
    """The one attribute the queue bodies read off a dequeued job, its encoded payload.

    payload: the encoded job body the body decodes with json.
    """

    payload: bytes


@pytest.fixture
def base_settings() -> Settings:
    """The default runtime configuration, the cadences and toggles the scheduler reads."""
    return Settings()


@pytest.fixture
def queue_seam(monkeypatch: pytest.MonkeyPatch) -> Callable[[object], RecordingQueue]:
    """A factory that swaps a module's queue seam for a fresh recorder, one per call.

    Returned as a callable so a property reinstalls a clean recorder for every example, keeping the
    enqueue log isolated per example while still patching through the test's own monkeypatch.

    monkeypatch: the pytest patcher the swap is registered on.
    """

    def install(module: object) -> RecordingQueue:
        return patch_queue_seam(monkeypatch, module)

    return install


@pytest.fixture
def pg_factory() -> type[RecordingPg]:
    """The recording PgQueuer application class, called to mint a fresh worker double per test."""
    return RecordingPg


@pytest.fixture
def job_factory() -> type[FakeJob]:
    """The dequeued-job double class, called with an encoded payload to drive a queue body."""
    return FakeJob


@pytest.fixture
def principal_factory() -> type[FakePrincipal]:
    """The roster-entry double class, called with an id to seed the fan-out roster."""
    return FakePrincipal
