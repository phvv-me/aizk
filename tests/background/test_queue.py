import asyncio
from collections.abc import Callable
from functools import partial
from types import ModuleType, SimpleNamespace
from typing import ClassVar, cast

import dbutil
import pytest
from asyncpg.exceptions import DuplicateObjectError, DuplicateTableError
from bg_doubles import RecordingPg, RecordingQueue, fake_runtime
from doubles import AsyncContext
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5, uuid5s, uuid7, uuid7s
from pgqueuer import PgQueuer
from pgqueuer.executors import DatabaseRetryEntrypointExecutor
from pgqueuer.models import Job
from pydantic import UUID5, UUID7

import aizk.background.jobs.projection as projection_mod
import aizk.background.queue as queue_mod
from aizk.background.jobs.models import ChunkJob
from aizk.background.jobs.projection import (
    ChunkProjectionJob,
    enqueue_document,
    enqueue_pending,
    retry_failed_chunks,
)
from aizk.background.queue import (
    Queue,
    QueueJob,
    QueuePayload,
    QueueSchema,
    install_queue_schema,
)
from aizk.config import settings
from aizk.graph.build import GraphClients
from aizk.store import Chunk, Watermark
from aizk.store.identity import User
from aizk.types import Scopes

InstallSeam = Callable[[ModuleType], RecordingQueue]
type FakeChunk = Chunk | SimpleNamespace | None


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


def test_queue_contract_dedupes_and_requeues_only_its_own_entrypoint(migrated_db: None) -> None:
    """Enqueue, dedupe, and the entrypoint-filtered requeue path against the real PgQueuer table.

    The schema was installed by `ops.setup`, so this exercises the true asyncpg driver, the
    partial unique dedupe index, and the SQL `entrypoint` filter that keeps one job type's
    retained failures from being requeued by another.
    """

    async def body() -> None:
        async with Queue(dsn=settings.asyncpg_dsn) as queue:
            table = queue.queries.qbe.settings.queue_table
            await queue.connection.execute(f"DELETE FROM {table}")
            assert isinstance(queue.worker(), PgQueuer)

            job = ExampleJob()
            other = cast("type[QueueJob[Payload]]", SimpleNamespace(entrypoint="other_entrypoint"))
            assert await job.enqueue(queue, Payload(value=7), "same") is True
            # The partial unique index rejects a second live job on the same dedupe key.
            assert await job.enqueue(queue, Payload(value=8), "same") is False

            # A merely queued job is not a retained failure.
            assert await queue.requeue_failed(ExampleJob, 3) == 0
            await queue.connection.execute(
                f"UPDATE {table} SET status = 'failed' WHERE dedupe_key = 'same'"
            )
            # The entrypoint filter hides this failure from a different job type.
            assert await queue.requeue_failed(other, 3) == 0
            assert await queue.requeue_failed(ExampleJob, 3) == 1
            # Requeue flipped it back to queued, so nothing failed remains.
            assert await queue.requeue_failed(ExampleJob, 3) == 0

    dbutil.run(body())


def test_queue_rejects_operations_before_its_connection_is_open() -> None:
    queue = Queue(dsn="postgresql://queue")

    with pytest.raises(RuntimeError, match="not open"):
        queue.worker()


@given(
    chunks=st.lists(uuid7s, max_size=5, unique=True),
    user=st.none() | uuid5s,
    limit=st.none() | st.integers(1, 4),
    source=st.none() | st.text(alphabet="abc", max_size=3),
)
def test_enqueue_pending_queues_one_deduped_job_per_chunk_and_counts_them(
    monkeypatch: pytest.MonkeyPatch,
    queue_seam: InstallSeam,
    chunks: list[UUID7],
    user: UUID5 | None,
    limit: int | None,
    source: str | None,
) -> None:
    recorder = queue_seam(projection_mod)
    seen_args: list[tuple[Scopes, int | None, str | None]] = []

    async def fake_pending(
        scopes: Scopes, chunk_limit: int | None, chunk_source: str | None
    ) -> list[SimpleNamespace]:
        seen_args.append((scopes, chunk_limit, chunk_source))
        return [SimpleNamespace(id=chunk) for chunk in chunks]

    monkeypatch.setattr(projection_mod, "pending_chunks", fake_pending)

    requested = frozenset({user}) if user is not None else None
    queued = asyncio.run(enqueue_pending(limit=limit, scopes=requested, source=source))

    resolved = frozenset({user or settings.system_user_id})
    assert seen_args == [(resolved, limit, source)]
    assert queued == len(chunks)
    assert {call.dedupe_key for call in recorder.enqueues} == {str(chunk) for chunk in chunks}
    assert all(call.entrypoint == ChunkProjectionJob.entrypoint for call in recorder.enqueues)
    assert all(call.priority == 50 for call in recorder.enqueues)
    assert recorder.opened == recorder.closed == 1
    assert ChunkProjectionJob.payload_type is ChunkJob


def test_enqueue_pending_swallows_the_already_queued_duplicate(
    monkeypatch: pytest.MonkeyPatch, queue_seam: InstallSeam
) -> None:
    recorder = queue_seam(projection_mod)
    chunk = SimpleNamespace(id=uuid7())

    async def fake_pending(scopes: Scopes, limit: int | None, source: str | None):
        return [chunk]

    monkeypatch.setattr(projection_mod, "pending_chunks", fake_pending)

    assert asyncio.run(enqueue_pending()) == 1
    assert asyncio.run(enqueue_pending()) == 0
    assert len(recorder.enqueues) == 1


def test_retry_failed_chunks_uses_the_typed_pgqueuer_boundary(
    queue_seam: InstallSeam,
) -> None:
    recorder = queue_seam(projection_mod)

    assert asyncio.run(retry_failed_chunks(limit=7)) == 4
    assert recorder.failed_requeues == [(ChunkProjectionJob.entrypoint, 7)]
    assert recorder.opened == recorder.closed == 1


def test_enqueue_document_targets_only_its_pending_chunks(
    monkeypatch: pytest.MonkeyPatch, queue_seam: InstallSeam
) -> None:
    recorder = queue_seam(projection_mod)
    document, owner = uuid7(), uuid5()
    chunks = [SimpleNamespace(id=uuid7()), SimpleNamespace(id=uuid7())]
    captured: list[tuple[Scopes, int | None, str | None, UUID7 | None]] = []

    async def fake_pending(
        scopes: Scopes,
        limit: int | None,
        source: str | None,
        document_id: UUID7 | None = None,
    ) -> list[SimpleNamespace]:
        captured.append((scopes, limit, source, document_id))
        return chunks

    monkeypatch.setattr(projection_mod, "pending_chunks", fake_pending)

    assert asyncio.run(enqueue_document(document, frozenset({owner}))) == 2
    assert captured == [(frozenset({owner}), None, None, document)]
    assert {call.dedupe_key for call in recorder.enqueues} == {str(chunk.id) for chunk in chunks}


class FakeSession:
    def __init__(self, chunk: FakeChunk) -> None:
        self.chunk = chunk

    async def get(self, model: type[Chunk], identifier: UUID7) -> FakeChunk:
        return self.chunk


def patch_chunk_pipeline(
    monkeypatch: pytest.MonkeyPatch, chunk: FakeChunk, touched: set[UUID5]
) -> list[tuple[Watermark.Kind, str]]:
    bumped: list[tuple[Watermark.Kind, str]] = []

    def fake_transaction(user: User) -> AsyncContext[FakeSession]:
        return AsyncContext(FakeSession(chunk))

    async def fake_extract(
        built_chunk: Chunk | SimpleNamespace, clients: GraphClients
    ) -> set[UUID5]:
        return touched

    async def fake_bump_many(
        session: FakeSession,
        scopes: Scopes,
        kind: Watermark.Kind,
        refs: list[str],
        by: int = 1,
    ) -> None:
        bumped.extend((kind, ref) for ref in refs)

    monkeypatch.setattr(User, "app", property(fake_transaction))
    monkeypatch.setattr(projection_mod, "extract_and_consolidate", fake_extract)
    monkeypatch.setattr(projection_mod.Watermark, "bump_many", fake_bump_many)
    return bumped


def chunk_projection_job() -> ChunkProjectionJob:
    return ChunkProjectionJob(fake_runtime().graph)


@given(touched=st.sets(uuid5s, max_size=4))
def test_process_chunk_dirties_exactly_the_entities_the_slice_touched(
    monkeypatch: pytest.MonkeyPatch, touched: set[UUID5]
) -> None:
    scope = uuid5()
    chunk = SimpleNamespace(text="some text", scopes=[scope], id=uuid7())
    bumped = patch_chunk_pipeline(monkeypatch, chunk, touched)

    asyncio.run(
        chunk_projection_job().handle(ChunkJob(chunk_id=chunk.id, scopes=frozenset({scope})))
    )

    assert bumped == [(Watermark.Kind.entity_dirty, str(entity)) for entity in touched]


def test_process_chunk_skips_a_chunk_it_cannot_see(monkeypatch: pytest.MonkeyPatch) -> None:
    extracted: list[Chunk | SimpleNamespace] = []

    def guard(built_chunk: Chunk | SimpleNamespace, clients: GraphClients) -> set[UUID5]:
        extracted.append(built_chunk)
        raise AssertionError("extract must not run for an invisible chunk")

    bumped = patch_chunk_pipeline(monkeypatch, None, set())
    monkeypatch.setattr(projection_mod, "extract_and_consolidate", guard)

    asyncio.run(
        chunk_projection_job().handle(ChunkJob(chunk_id=uuid7(), scopes=frozenset({uuid5()})))
    )
    assert bumped == [] and extracted == []

    wrong_scope = SimpleNamespace(text="some text", scopes=[uuid5()], id=uuid7())
    bumped = patch_chunk_pipeline(monkeypatch, wrong_scope, set())
    asyncio.run(
        chunk_projection_job().handle(
            ChunkJob(chunk_id=wrong_scope.id, scopes=frozenset({uuid5()}))
        )
    )
    assert bumped == []


@pytest.mark.parametrize("index_current", [False, True])
@pytest.mark.parametrize("install_error", [None, DuplicateObjectError, DuplicateTableError])
def test_install_queue_schema_grants_only_discovered_objects_re_install_tolerated(
    monkeypatch: pytest.MonkeyPatch,
    install_error: type[Exception] | None,
    index_current: bool,
) -> None:
    grants: list[str] = []
    checks: list[tuple[str, str, str]] = []
    upgrades: list[bool] = []

    async def execute(sql: str) -> None:
        grants.append(sql)

    async def fetchval(sql: str, table: str, index: str) -> bool:
        checks.append((sql, table, index))
        return index_current

    async def close() -> None:
        return None

    async def connect(dsn: str) -> SimpleNamespace:
        assert dsn == settings.admin_asyncpg_dsn
        return SimpleNamespace(execute=execute, fetchval=fetchval, close=close)

    class FakeQueries:
        def __init__(self, driver: SimpleNamespace) -> None:
            self.driver = driver
            self.qbe = SimpleNamespace(
                settings=SimpleNamespace(
                    queue_table="custom_queue",
                    queue_table_log="custom_log",
                    statistics_table="custom_statistics",
                    schedules_table="custom_schedules",
                    queue_status_type="custom_queue_status",
                )
            )

        async def install(self) -> None:
            if install_error is not None:
                raise install_error("already there")

        async def upgrade(self) -> None:
            upgrades.append(True)

    monkeypatch.setattr(queue_mod, "asyncpg", SimpleNamespace(connect=connect))
    monkeypatch.setattr(queue_mod, "AsyncpgDriver", lambda connection: connection)
    monkeypatch.setattr(queue_mod, "Queries", FakeQueries)
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://writer@host:5432/db")

    asyncio.run(install_queue_schema())

    granted = " ".join(grants)
    assert grants[0].startswith("SELECT pg_advisory_lock")
    [(definition_check, checked_table, checked_index)] = checks
    assert checked_table == "custom_queue"
    assert checked_index == "custom_queue_unique_dedupe_key"
    assert "FROM pg_indexes AS indexes" in definition_check
    assert "pg_get_expr" in definition_check
    assert all(status in definition_check for status in ("queued", "picked", "failed"))
    rebuilds = [
        statement
        for statement in grants
        if statement.startswith("DROP INDEX") or statement.startswith("CREATE UNIQUE INDEX")
    ]
    assert len(rebuilds) == (0 if index_current else 2)
    role_grants = [statement for statement in grants if statement.startswith("GRANT")]
    assert all("writer" in grant for grant in role_grants)
    assert all("ALL TABLES" not in grant and "ALL SEQUENCES" not in grant for grant in role_grants)
    assert all(
        name in granted
        for name in (
            "custom_queue",
            "custom_log",
            "custom_statistics",
            "custom_schedules",
            "custom_queue_id_seq",
            "custom_log_id_seq",
            "custom_statistics_id_seq",
            "custom_schedules_id_seq",
        )
    )
    assert upgrades == ([True] if install_error is not None else [])


def test_simultaneous_installs_serialize_on_the_advisory_lock(migrated_db: None) -> None:
    """Two services starting at the same instant must not race the dedupe index rebuild."""

    async def race() -> None:
        await asyncio.gather(install_queue_schema(), install_queue_schema())

    dbutil.run(race())


def test_later_replica_skips_the_current_index_while_another_keeps_serving(
    migrated_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A steady-state startup must neither block a reader nor weaken live deduplication."""

    async def body() -> None:
        async with Queue(dsn=settings.asyncpg_dsn) as serving:
            names = serving.queries.qbe.settings
            schema = QueueSchema(
                queue=names.queue_table,
                log=names.queue_table_log,
                statistics=names.statistics_table,
                schedules=names.schedules_table,
                status_type=names.queue_status_type,
            )

            async def already_installed(_queue: Queue) -> QueueSchema:
                return schema

            async def no_grants(*_args: object) -> None:
                return None

            monkeypatch.setattr(Queue, "install", already_installed)
            monkeypatch.setattr(queue_mod, "grant_queue_access", no_grants)

            await serving.connection.execute(f"DELETE FROM {schema.queue}")
            job = ExampleJob()
            assert await job.enqueue(serving, Payload(value=1), "serving") is True

            async with serving.connection.transaction():
                assert (
                    await serving.connection.fetchval(
                        f"SELECT dedupe_key FROM {schema.queue} WHERE dedupe_key = $1",
                        "serving",
                    )
                    == "serving"
                )
                await asyncio.wait_for(install_queue_schema(), timeout=5)

            assert await job.enqueue(serving, Payload(value=2), "serving") is False

    dbutil.run(body())
