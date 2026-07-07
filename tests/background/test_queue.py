import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
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
from aizk.store import Watermark

uuids = st.uuids()
InstallSeam = Callable[[object], RecordingQueue]


@given(entities=st.lists(uuids, max_size=5, unique=True), principal=uuids)
def test_enqueue_profiles_debounces_per_entity_and_skips_when_empty(
    queue_seam: InstallSeam, entities: list[uuid.UUID], principal: uuid.UUID
) -> None:
    """Each touched entity gets one rebuild job keyed on it, an empty set opens no connection.

    A second pass over the same entities re-enqueues nothing, each dedupe key colliding with the
    already-queued job so the `DuplicateJobError` skip swallows it, the debounce that collapses a
    write burst touching one entity into a single rebuild while the first is still in flight.
    """
    recorder = queue_seam(queue_mod)

    asyncio.run(enqueue_profiles(entities, principal))
    asyncio.run(enqueue_profiles(entities, principal))  # every key already queued, all skipped

    assert len(recorder.enqueues) == len(entities)
    assert {call.dedupe_key for call in recorder.enqueues} == {f"profile:{e}" for e in entities}
    assert all(call.entrypoint == PROFILE_ENTRYPOINT for call in recorder.enqueues)
    assert recorder.opened == recorder.closed == (2 if entities else 0)


@given(
    chunks=st.lists(uuids, max_size=5, unique=True),
    principal=st.none() | uuids,
    limit=st.none() | st.integers(1, 4),
    source=st.none() | st.text(alphabet="abc", max_size=3),
)
def test_enqueue_pending_queues_one_deduped_job_per_chunk_and_counts_them(
    monkeypatch: pytest.MonkeyPatch,
    queue_seam: InstallSeam,
    chunks: list[uuid.UUID],
    principal: uuid.UUID | None,
    limit: int | None,
    source: str | None,
) -> None:
    """Every pending chunk the lister returns enqueues one deduped extraction job, counted once.

    A null principal falls back to the system identity the lister is then queried under, and the
    chunk id is the dedupe key so re-enqueuing the same backlog is harmless. The lister arguments
    are captured to prove the caller's limit, source, and resolved principal reach it verbatim.
    """
    recorder = queue_seam(queue_mod)
    seen_args: list[tuple[uuid.UUID, int | None, str | None]] = []

    async def fake_pending(
        principal_id: uuid.UUID, chunk_limit: int | None, chunk_source: str | None
    ) -> list[SimpleNamespace]:
        seen_args.append((principal_id, chunk_limit, chunk_source))
        return [SimpleNamespace(id=chunk) for chunk in chunks]

    monkeypatch.setattr(queue_mod, "pending_chunks", fake_pending)

    queued = asyncio.run(enqueue_pending(limit=limit, principal_id=principal, source=source))

    resolved = principal or settings.system_user_id
    assert seen_args == [(resolved, limit, source)]
    assert queued == len(chunks)
    assert {call.dedupe_key for call in recorder.enqueues} == {str(chunk) for chunk in chunks}
    assert all(call.entrypoint == EXTRACT_ENTRYPOINT for call in recorder.enqueues)
    assert recorder.opened == recorder.closed == 1


def test_enqueue_pending_swallows_the_already_queued_duplicate(
    monkeypatch: pytest.MonkeyPatch, queue_seam: InstallSeam
) -> None:
    """A second pass over the same pending chunk skips the duplicate and reports only fresh work.

    The recorder raises `DuplicateJobError` on a repeated dedupe key the way pgqueuer does, so the
    harmless-re-enqueue contract must swallow it and count zero rather than crash the caller.
    """
    recorder = queue_seam(queue_mod)
    chunk = SimpleNamespace(id=uuid.uuid4())

    async def fake_pending(principal_id: uuid.UUID, limit: int | None, source: str | None):
        return [chunk]

    monkeypatch.setattr(queue_mod, "pending_chunks", fake_pending)

    assert asyncio.run(enqueue_pending()) == 1
    assert asyncio.run(enqueue_pending()) == 0
    assert len(recorder.enqueues) == 1


class FakeSession:
    """A scoped session stand-in whose only read is the chunk the build body loads.

    chunk: the object `session.get` returns for the build, or None when the chunk is invisible.
    """

    def __init__(self, chunk: object) -> None:
        self.chunk = chunk

    async def get(self, model: object, identifier: uuid.UUID) -> object:
        """Return the seeded chunk for the build's lookup, ignoring the fixed model and id.

        model: the ORM class the body asks for, ignored since the chunk is fixed.
        identifier: the chunk id, ignored for the same reason.
        """
        return self.chunk


def patch_chunk_pipeline(
    monkeypatch: pytest.MonkeyPatch, chunk: object, touched: set[uuid.UUID]
) -> list[tuple[Watermark.Kind, str]]:
    """Swap the visibility, extraction, and watermark seams the per-chunk build glues together.

    The build's own logic is the glue: load the chunk under the owner, hand it to
    `extract_and_consolidate`, then bump a dirty watermark per touched entity. The core build and
    the watermark writes are mocked so only that glue is under test, and the returned log records
    every dirty bump so a test asserts the touched-set bookkeeping without a model or database.

    monkeypatch: the pytest patcher.
    chunk: the chunk `session.get` hands the build, or None to drive the invisible-chunk branch.
    touched: the entity ids the mocked core reports the slice touched.
    """
    bumped: list[tuple[Watermark.Kind, str]] = []

    @asynccontextmanager
    async def fake_acting_as(principal_id: uuid.UUID) -> AsyncIterator[FakeSession]:
        yield FakeSession(chunk)

    async def fake_extract(built_chunk: object, principal_id: uuid.UUID) -> set[uuid.UUID]:
        return touched

    async def fake_bump(
        session: object,
        owner_id: uuid.UUID,
        kind: Watermark.Kind,
        ref: str = "global",
        by: int = 1,
    ) -> int:
        bumped.append((kind, ref))
        return 1

    monkeypatch.setattr(queue_mod, "acting_as", fake_acting_as)
    monkeypatch.setattr(queue_mod, "extract_and_consolidate", fake_extract)
    monkeypatch.setattr(queue_mod.Watermark, "bump", fake_bump)
    return bumped


@given(touched=st.sets(uuids, max_size=4))
def test_process_chunk_dirties_exactly_the_entities_the_slice_touched(
    monkeypatch: pytest.MonkeyPatch, touched: set[uuid.UUID]
) -> None:
    """A visible chunk bumps one dirty watermark per touched entity and returns that same set.

    An empty touched set takes the no-bump branch, so the returned list and the dirty log stay
    empty, while any touched entity is dirtied under `entity_dirty` keyed on its own id, the
    on-write signal a debounced profile rebuild reads.
    """
    chunk = SimpleNamespace(text="some text", scope=None, id=uuid.uuid4())
    bumped = patch_chunk_pipeline(monkeypatch, chunk, touched)

    result = asyncio.run(process_chunk(uuid.uuid4(), uuid.uuid4()))

    assert set(result) == touched
    assert bumped == [(Watermark.Kind.entity_dirty, str(entity)) for entity in result]


def test_process_chunk_skips_a_chunk_it_cannot_see(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chunk invisible under the owner's row level security yields no slice and dirties nothing.

    When `session.get` returns nothing the body returns early, so the core build never runs and no
    watermark is bumped, the no-leak skip the queue path takes rather than acting on a stray id.
    """
    extracted: list[object] = []

    def guard(built_chunk: object, principal_id: uuid.UUID) -> set[uuid.UUID]:
        extracted.append(built_chunk)
        raise AssertionError("extract must not run for an invisible chunk")

    bumped = patch_chunk_pipeline(monkeypatch, None, set())
    monkeypatch.setattr(queue_mod, "extract_and_consolidate", guard)

    assert asyncio.run(process_chunk(uuid.uuid4(), uuid.uuid4())) == []
    assert bumped == [] and extracted == []


def test_process_profile_rebuilds_then_clears_the_dirty_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebuilding one profile clears that entity's dirty counter back to zero under its owner."""
    entity, principal = uuid.uuid4(), uuid.uuid4()
    built: list[tuple[uuid.UUID, uuid.UUID]] = []
    cleared: list[tuple[Watermark.Kind, int, str]] = []

    async def fake_build_profile(entity_id: uuid.UUID, principal_id: uuid.UUID) -> None:
        built.append((entity_id, principal_id))

    @asynccontextmanager
    async def fake_acting_as(principal_id: uuid.UUID) -> AsyncIterator[None]:
        yield None

    async def fake_set_value(
        session: object,
        owner_id: uuid.UUID,
        kind: Watermark.Kind,
        counter: int = 0,
        payload: object = None,
        ref: str = "global",
    ) -> None:
        cleared.append((kind, counter, ref))

    monkeypatch.setattr(queue_mod, "build_profile", fake_build_profile)
    monkeypatch.setattr(queue_mod, "acting_as", fake_acting_as)
    monkeypatch.setattr(queue_mod.Watermark, "set_value", fake_set_value)

    asyncio.run(process_profile(entity, principal))

    assert built == [(entity, principal)]
    assert cleared == [(Watermark.Kind.entity_dirty, 0, str(entity))]


@pytest.mark.parametrize("install_error", [None, DuplicateObjectError, DuplicateTableError])
def test_install_queue_schema_grants_every_table_and_sequence_re_install_tolerated(
    monkeypatch: pytest.MonkeyPatch, install_error: type[Exception] | None
) -> None:
    """Installing the queue grants the app role each table and sequence, a re-install swallowed.

    A pre-existing schema surfaces as `DuplicateObjectError`/`DuplicateTableError` from `install`,
    which the body treats as already-installed and still runs every grant, so the restricted role
    can read and write the queue whether the install is the first or the tenth.
    """
    grants: list[str] = []

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

    monkeypatch.setattr(queue_mod, "asyncpg", SimpleNamespace(connect=connect))
    monkeypatch.setattr(queue_mod, "AsyncpgDriver", lambda connection: connection)
    monkeypatch.setattr(queue_mod, "Queries", FakeQueries)
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://writer@host:5432/db")

    asyncio.run(install_queue_schema())

    granted = " ".join(grants)
    for name in (*QUEUE_TABLES, *QUEUE_SEQUENCES):
        assert name in granted
    assert all("writer" in grant for grant in grants)


def test_queue_connection_opens_and_closes_exactly_once(queue_seam: InstallSeam) -> None:
    """The shared connection scope opens once on entry and closes once when the block exits."""
    recorder = queue_seam(queue_mod)

    async def drive() -> None:
        async with queue_mod.queue_connection():
            assert recorder.opened == 1
            assert recorder.closed == 0

    asyncio.run(drive())
    assert recorder.opened == recorder.closed == 1
