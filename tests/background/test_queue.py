import asyncio
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from asyncpg.exceptions import DuplicateObjectError, DuplicateTableError
from bg_doubles import RecordingQueue
from hypothesis import given
from hypothesis import strategies as st

import aizk.background.queue as queue_mod
from aizk.background.queue import (
    EXTRACT_ENTRYPOINT,
    PROFILE_ENTRYPOINT,
    QUEUE_SEQUENCES,
    QUEUE_TABLES,
    enqueue_pending,
    enqueue_profiles,
    install_queue_schema,
    process_chunk,
    process_profile,
)
from aizk.config import settings
from aizk.store import Watermark, engine
from aizk.store.identity import User
from aizk.types import Scopes

uuids = st.uuids()
InstallSeam = Callable[[object], RecordingQueue]


@given(entities=st.lists(uuids, max_size=5, unique=True), user=uuids)
def test_enqueue_profiles_debounces_per_entity_and_skips_when_empty(
    queue_seam: InstallSeam, entities: list[uuid.UUID], user: uuid.UUID
) -> None:
    recorder = queue_seam(queue_mod)

    asyncio.run(enqueue_profiles(entities, frozenset({user})))
    asyncio.run(enqueue_profiles(entities, frozenset({user})))

    assert len(recorder.enqueues) == len(entities)
    assert {call.dedupe_key for call in recorder.enqueues} == {
        f"profile:{user}:{entity}" for entity in entities
    }
    assert all(call.entrypoint == PROFILE_ENTRYPOINT for call in recorder.enqueues)
    assert recorder.opened == recorder.closed == (2 if entities else 0)


@given(
    chunks=st.lists(uuids, max_size=5, unique=True),
    user=st.none() | uuids,
    limit=st.none() | st.integers(1, 4),
    source=st.none() | st.text(alphabet="abc", max_size=3),
)
def test_enqueue_pending_queues_one_deduped_job_per_chunk_and_counts_them(
    monkeypatch: pytest.MonkeyPatch,
    queue_seam: InstallSeam,
    chunks: list[uuid.UUID],
    user: uuid.UUID | None,
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
    assert all(call.entrypoint == EXTRACT_ENTRYPOINT for call in recorder.enqueues)
    assert recorder.opened == recorder.closed == 1


def test_enqueue_pending_swallows_the_already_queued_duplicate(
    monkeypatch: pytest.MonkeyPatch, queue_seam: InstallSeam
) -> None:
    recorder = queue_seam(queue_mod)
    chunk = SimpleNamespace(id=uuid.uuid4())

    async def fake_pending(scopes: Scopes, limit: int | None, source: str | None):
        return [chunk]

    monkeypatch.setattr(queue_mod, "pending_chunks", fake_pending)

    assert asyncio.run(enqueue_pending()) == 1
    assert asyncio.run(enqueue_pending()) == 0
    assert len(recorder.enqueues) == 1


class FakeSession:
    def __init__(self, chunk: object) -> None:
        self.chunk = chunk

    async def get(self, model: object, identifier: uuid.UUID) -> object:
        return self.chunk


def patch_chunk_pipeline(
    monkeypatch: pytest.MonkeyPatch, chunk: object, touched: set[uuid.UUID]
) -> list[tuple[Watermark.Kind, str]]:
    bumped: list[tuple[Watermark.Kind, str]] = []

    @asynccontextmanager
    async def fake_transaction(user: User) -> AsyncGenerator[FakeSession]:
        yield FakeSession(chunk)

    async def fake_extract(built_chunk: object) -> set[uuid.UUID]:
        return touched

    async def fake_bump_many(
        session: object,
        scopes: Scopes,
        kind: Watermark.Kind,
        refs: list[str],
        by: int = 1,
    ) -> None:
        bumped.extend((kind, ref) for ref in refs)

    monkeypatch.setattr(engine, "transaction", fake_transaction)
    monkeypatch.setattr(queue_mod, "extract_and_consolidate", fake_extract)
    monkeypatch.setattr(queue_mod.Watermark, "bump_many", fake_bump_many)
    return bumped


@given(touched=st.sets(uuids, max_size=4))
def test_process_chunk_dirties_exactly_the_entities_the_slice_touched(
    monkeypatch: pytest.MonkeyPatch, touched: set[uuid.UUID]
) -> None:
    scope = uuid.uuid4()
    chunk = SimpleNamespace(text="some text", scopes=[scope], id=uuid.uuid4())
    bumped = patch_chunk_pipeline(monkeypatch, chunk, touched)

    result = asyncio.run(process_chunk(uuid.uuid4(), frozenset({scope})))

    assert set(result) == touched
    assert bumped == [(Watermark.Kind.entity_dirty, str(entity)) for entity in result]


def test_process_chunk_skips_a_chunk_it_cannot_see(monkeypatch: pytest.MonkeyPatch) -> None:
    extracted: list[object] = []

    def guard(built_chunk: object) -> set[uuid.UUID]:
        extracted.append(built_chunk)
        raise AssertionError("extract must not run for an invisible chunk")

    bumped = patch_chunk_pipeline(monkeypatch, None, set())
    monkeypatch.setattr(queue_mod, "extract_and_consolidate", guard)

    assert asyncio.run(process_chunk(uuid.uuid4(), frozenset({uuid.uuid4()}))) == []
    assert bumped == [] and extracted == []

    wrong_scope = SimpleNamespace(text="some text", scopes=[uuid.uuid4()], id=uuid.uuid4())
    bumped = patch_chunk_pipeline(monkeypatch, wrong_scope, set())
    assert asyncio.run(process_chunk(uuid.uuid4(), frozenset({uuid.uuid4()}))) == []
    assert bumped == []


def test_process_profile_rebuilds_then_clears_the_dirty_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity, user = uuid.uuid4(), uuid.uuid4()
    built: list[tuple[uuid.UUID, Scopes]] = []
    cleared: list[tuple[Watermark.Kind, int, str]] = []

    async def fake_build_profile(entity_id: uuid.UUID, scopes: Scopes) -> None:
        built.append((entity_id, scopes))

    @asynccontextmanager
    async def fake_transaction(user: User) -> AsyncGenerator[None]:
        yield None

    async def fake_set_value(
        session: object,
        scopes: Scopes,
        kind: Watermark.Kind,
        counter: int = 0,
        payload: object = None,
        ref: str = "global",
    ) -> None:
        cleared.append((kind, counter, ref))

    monkeypatch.setattr(queue_mod, "build_profile", fake_build_profile)
    monkeypatch.setattr(engine, "transaction", fake_transaction)
    monkeypatch.setattr(queue_mod.Watermark, "set_value", fake_set_value)

    key = frozenset({user})
    asyncio.run(process_profile(entity, key))

    assert built == [(entity, key)]
    assert cleared == [(Watermark.Kind.entity_dirty, 0, str(entity))]


@pytest.mark.parametrize("install_error", [None, DuplicateObjectError, DuplicateTableError])
def test_install_queue_schema_grants_every_table_and_sequence_re_install_tolerated(
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
        def __init__(self, driver: object) -> None:
            self.driver = driver

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
    for name in (*QUEUE_TABLES, *QUEUE_SEQUENCES):
        assert name in granted
    assert all("writer" in grant for grant in grants)
    assert upgrades == ([True] if install_error is not None else [])


def test_queue_connection_opens_and_closes_exactly_once(queue_seam: InstallSeam) -> None:
    recorder = queue_seam(queue_mod)

    async def drive() -> None:
        async with queue_mod.queue_connection():
            assert recorder.opened == 1
            assert recorder.closed == 0

    asyncio.run(drive())
    assert recorder.opened == recorder.closed == 1
