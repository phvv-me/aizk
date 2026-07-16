import asyncio
from collections.abc import Callable
from types import ModuleType, SimpleNamespace

import pytest
from asyncpg.exceptions import DuplicateObjectError, DuplicateTableError
from bg_doubles import RecordingQueue
from doubles import AsyncContext
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5, uuid5s, uuid7, uuid7s
from pydantic import UUID5, UUID7

import aizk.background.jobs.projection as projection_mod
import aizk.background.queue as queue_mod
from aizk.background.jobs.models import ChunkJob
from aizk.background.jobs.projection import ChunkProjectionJob
from aizk.background.queue import (
    enqueue_document,
    enqueue_pending,
    install_queue_schema,
    retry_failed_chunks,
)
from aizk.config import settings
from aizk.store import Chunk, Watermark
from aizk.store.identity import User
from aizk.types import Scopes

InstallSeam = Callable[[ModuleType], RecordingQueue]
type FakeChunk = Chunk | SimpleNamespace | None


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
    recorder = queue_seam(queue_mod)
    seen_args: list[tuple[Scopes, int | None, str | None]] = []

    async def fake_pending(
        scopes: Scopes, chunk_limit: int | None, chunk_source: str | None
    ) -> list[SimpleNamespace]:
        seen_args.append((scopes, chunk_limit, chunk_source))
        return [SimpleNamespace(id=chunk) for chunk in chunks]

    monkeypatch.setattr(queue_mod, "pending_chunks", fake_pending)

    requested = frozenset({user}) if user is not None else None
    queued = asyncio.run(enqueue_pending(limit=limit, scopes=requested, source=source))

    resolved = frozenset({user or settings.system_user_id})
    assert seen_args == [(resolved, limit, source)]
    assert queued == len(chunks)
    assert {call.dedupe_key for call in recorder.enqueues} == {str(chunk) for chunk in chunks}
    assert all(call.entrypoint == ChunkProjectionJob().entrypoint for call in recorder.enqueues)
    assert all(call.priority == 50 for call in recorder.enqueues)
    assert recorder.opened == recorder.closed == 1
    assert ChunkProjectionJob().payload_type is ChunkJob


def test_enqueue_pending_swallows_the_already_queued_duplicate(
    monkeypatch: pytest.MonkeyPatch, queue_seam: InstallSeam
) -> None:
    recorder = queue_seam(queue_mod)
    chunk = SimpleNamespace(id=uuid7())

    async def fake_pending(scopes: Scopes, limit: int | None, source: str | None):
        return [chunk]

    monkeypatch.setattr(queue_mod, "pending_chunks", fake_pending)

    assert asyncio.run(enqueue_pending()) == 1
    assert asyncio.run(enqueue_pending()) == 0
    assert len(recorder.enqueues) == 1


def test_retry_failed_chunks_uses_the_typed_pgqueuer_boundary(
    queue_seam: InstallSeam,
) -> None:
    recorder = queue_seam(queue_mod)

    assert asyncio.run(retry_failed_chunks(limit=7)) == 4
    assert recorder.failed_requeues == [(ChunkProjectionJob.entrypoint, 7)]
    assert recorder.opened == recorder.closed == 1


def test_enqueue_document_targets_only_its_pending_chunks(
    monkeypatch: pytest.MonkeyPatch, queue_seam: InstallSeam
) -> None:
    recorder = queue_seam(queue_mod)
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

    monkeypatch.setattr(queue_mod, "pending_chunks", fake_pending)

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

    async def fake_extract(built_chunk: Chunk | SimpleNamespace) -> set[UUID5]:
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


@given(touched=st.sets(uuid5s, max_size=4))
def test_process_chunk_dirties_exactly_the_entities_the_slice_touched(
    monkeypatch: pytest.MonkeyPatch, touched: set[UUID5]
) -> None:
    scope = uuid5()
    chunk = SimpleNamespace(text="some text", scopes=[scope], id=uuid7())
    bumped = patch_chunk_pipeline(monkeypatch, chunk, touched)

    asyncio.run(
        ChunkProjectionJob().handle(ChunkJob(chunk_id=chunk.id, scopes=frozenset({scope})))
    )

    assert bumped == [(Watermark.Kind.entity_dirty, str(entity)) for entity in touched]


def test_process_chunk_skips_a_chunk_it_cannot_see(monkeypatch: pytest.MonkeyPatch) -> None:
    extracted: list[Chunk | SimpleNamespace] = []

    def guard(built_chunk: Chunk | SimpleNamespace) -> set[UUID5]:
        extracted.append(built_chunk)
        raise AssertionError("extract must not run for an invisible chunk")

    bumped = patch_chunk_pipeline(monkeypatch, None, set())
    monkeypatch.setattr(projection_mod, "extract_and_consolidate", guard)

    asyncio.run(
        ChunkProjectionJob().handle(ChunkJob(chunk_id=uuid7(), scopes=frozenset({uuid5()})))
    )
    assert bumped == [] and extracted == []

    wrong_scope = SimpleNamespace(text="some text", scopes=[uuid5()], id=uuid7())
    bumped = patch_chunk_pipeline(monkeypatch, wrong_scope, set())
    asyncio.run(
        ChunkProjectionJob().handle(ChunkJob(chunk_id=wrong_scope.id, scopes=frozenset({uuid5()})))
    )
    assert bumped == []


@pytest.mark.parametrize("install_error", [None, DuplicateObjectError, DuplicateTableError])
def test_install_queue_schema_grants_only_discovered_objects_re_install_tolerated(
    monkeypatch: pytest.MonkeyPatch, install_error: type[Exception] | None
) -> None:
    grants: list[str] = []
    upgrades: list[bool] = []

    async def execute(sql: str) -> None:
        grants.append(sql)

    async def close() -> None:
        return None

    async def connect(dsn: str) -> SimpleNamespace:
        assert dsn == settings.admin_asyncpg_dsn
        return SimpleNamespace(execute=execute, close=close)

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
    assert "custom_queue_unique_dedupe_key" in granted
    assert all(status in granted for status in ("queued", "picked", "failed"))
    assert upgrades == ([True] if install_error is not None else [])


def test_queue_connection_opens_and_closes_exactly_once(queue_seam: InstallSeam) -> None:
    recorder = queue_seam(queue_mod)

    async def drive() -> None:
        async with queue_mod.Queue(dsn=settings.asyncpg_dsn):
            assert recorder.opened == 1
            assert recorder.closed == 0

    asyncio.run(drive())
    assert recorder.opened == recorder.closed == 1
