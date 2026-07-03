import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from asyncpg.exceptions import DuplicateObjectError, DuplicateTableError
from hypothesis import given
from hypothesis import strategies as st

import aizk.background.queue as queue_mod
from aizk.background.payloads import ChunkJob, ProfileJob, TaskJob
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


@pytest.mark.parametrize(
    ("payload_cls", "field"),
    [(ChunkJob, "chunk_id"), (ProfileJob, "entity_id"), (TaskJob, None)],
    ids=["chunk", "profile", "task"],
)
@given(first=uuids, second=uuids)
def test_payload_round_trips_exactly_the_fields_the_worker_decodes(
    payload_cls: type[ChunkJob | ProfileJob | TaskJob],
    field: str | None,
    first: uuid.UUID,
    second: uuid.UUID,
) -> None:
    """Each queue payload decodes back to exactly the ids its worker body reads, unchanged.

    The chunk and profile jobs carry their own subject plus the principal, and the task job carries
    only the principal, so a round trip through `encode`/`decode` reproduces the fields as built.
    """
    job = (
        payload_cls(**{field: first, "principal_id": second})
        if field
        else payload_cls(principal_id=second)
    )
    decoded = payload_cls.decode(job.encode())
    assert decoded == job
    assert decoded.principal_id == second
    if field:
        assert getattr(decoded, field) == first


@given(
    scheme=st.sampled_from(["postgresql", "postgres"]),
    rest=st.text(alphabet="abcdefghijklmnop/@:._-", min_size=1, max_size=30),
)
def test_asyncpg_dsn_drops_the_asyncpg_tag_once_and_is_idempotent(
    scheme: str, rest: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stripping the driver tag yields a clean asyncpg DSN and re-stripping leaves it untouched."""
    tagged = f"{scheme}+asyncpg://{rest}"
    clean = f"{scheme}://{rest}"
    monkeypatch.setattr(settings, "database_url", tagged)
    assert settings.asyncpg_dsn == clean
    monkeypatch.setattr(settings, "database_url", clean)
    assert settings.asyncpg_dsn == clean


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql+asyncpg://writer@host:5432/db", "writer"),
        ("postgresql+asyncpg://host:5432/db", "aizk_app"),
    ],
)
def test_app_role_reads_the_username_or_falls_back(
    url: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restricted role is the DSN username, defaulting to aizk_app when the DSN omits it."""
    monkeypatch.setattr(settings, "database_url", url)
    assert settings.app_role == expected


@given(entities=st.lists(uuids, max_size=5, unique=True), principal=uuids)
def test_enqueue_profiles_debounces_per_entity_and_skips_when_empty(
    queue_seam, entities: list[uuid.UUID], principal: uuid.UUID
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


@given(chunks=st.lists(uuids, max_size=5, unique=True), principal=uuids)
def test_enqueue_pending_queues_one_job_per_chunk_and_returns_the_count(
    monkeypatch: pytest.MonkeyPatch, queue_seam, chunks: list[uuid.UUID], principal: uuid.UUID
) -> None:
    """Every pending chunk the lister returns enqueues one deduped job, counted in the total."""
    recorder = queue_seam(queue_mod)

    async def fake_pending(
        principal_id: uuid.UUID,
        limit: int | None,
        source: str | None,
    ) -> list[SimpleNamespace]:
        return [SimpleNamespace(id=chunk) for chunk in chunks]

    monkeypatch.setattr(queue_mod, "pending_chunks", fake_pending)

    queued = asyncio.run(enqueue_pending(principal_id=principal))

    assert queued == len(chunks)
    assert {call.dedupe_key for call in recorder.enqueues} == {str(chunk) for chunk in chunks}
    assert all(call.entrypoint == EXTRACT_ENTRYPOINT for call in recorder.enqueues)


class FakeSession:
    """A scoped session stand-in whose only read is the chunk the build body loads.

    chunk: the object `session.get` returns for the build, or None when invisible.
    """

    def __init__(self, chunk: object) -> None:
        self.chunk = chunk

    async def get(self, model: object, identifier: uuid.UUID) -> object:
        """Return the seeded chunk for the build's lookup.

        model: the ORM class the body asks for, ignored since the chunk is fixed.
        identifier: the chunk id, ignored for the same reason.
        """
        return self.chunk


def patch_chunk_pipeline(monkeypatch: pytest.MonkeyPatch, chunk: object) -> dict[str, list]:
    """Swap every extraction, resolution, and watermark seam the per-chunk build calls.

    Records the consolidated facts and the bumped entity refs so a test asserts the build's own
    glue, the touched-set bookkeeping, without any model, LLM, or database.

    monkeypatch: the pytest patcher.
    chunk: the chunk `session.get` hands the build, or None to drive the invisible-chunk branch.
    """
    log: dict[str, list] = {"consolidated": [], "bumped": []}

    @asynccontextmanager
    async def fake_acting_as(principal_id: uuid.UUID) -> AsyncIterator[FakeSession]:
        yield FakeSession(chunk)

    monkeypatch.setattr(queue_mod, "acting_as", fake_acting_as)

    async def fake_extract(text: str) -> SimpleNamespace:
        return SimpleNamespace(
            entities=[
                SimpleNamespace(name="known", type="person"),
                SimpleNamespace(name="skip", type="person"),
            ],
            facts=["raw"],
        )

    async def fake_resolve_timestamps(text: str, facts: list) -> list[str]:
        return ["dated"]

    resolved = {"known": uuid.uuid4(), "skip": None}

    class FakeWriter:
        """Stand-in for GraphWriter, recording each consolidated fact without the real body."""

        def __init__(self, session: object, owner_id: uuid.UUID, scope: object) -> None:
            pass

        async def resolve(self, name: str, kind: str) -> uuid.UUID | None:
            return resolved[name]

        async def consolidate(self, fact: object, chunk_id: uuid.UUID) -> None:
            log["consolidated"].append(fact)

    async def fake_bump(
        session: object, owner_id: uuid.UUID, kind: str, ref: str = "global", by: int = 1
    ) -> int:
        log["bumped"].append((kind, ref))
        return 1

    monkeypatch.setattr(queue_mod, "extract_triples", fake_extract)
    monkeypatch.setattr(queue_mod, "resolve_timestamps", fake_resolve_timestamps)
    monkeypatch.setattr(queue_mod, "GraphWriter", FakeWriter)
    monkeypatch.setattr(queue_mod.Watermark, "bump", fake_bump)
    log["resolved_known"] = [resolved["known"]]
    return log


@pytest.mark.parametrize("visible", [True, False], ids=["resolved", "invisible"])
def test_process_chunk_touches_what_it_resolves_and_skips_what_it_cannot_see(
    monkeypatch: pytest.MonkeyPatch, visible: bool
) -> None:
    """A visible chunk resolves entities, consolidates each dated fact, and dirties what it hit.

    A chunk invisible under the owner's row level security yields no graph slice, so the touched
    set, the consolidation log, and the dirty bumps are all empty, the no-leak skip the same body
    takes when `session.get` returns nothing.
    """
    chunk = SimpleNamespace(text="some text", scope=None, id=uuid.uuid4()) if visible else None
    log = patch_chunk_pipeline(monkeypatch, chunk)
    chunk_id, principal = uuid.uuid4(), uuid.uuid4()

    touched = asyncio.run(process_chunk(chunk_id, principal))

    if visible:
        assert touched == log["resolved_known"]
        assert log["consolidated"] == ["dated"]
        assert log["bumped"] == [(Watermark.Kind.entity_dirty, str(touched[0]))]
    else:
        assert touched == []
        assert log["consolidated"] == [] and log["bumped"] == []


def test_process_profile_rebuilds_then_clears_the_dirty_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebuilding one profile clears that entity's dirty counter back to zero."""
    entity, principal = uuid.uuid4(), uuid.uuid4()
    built: list[uuid.UUID] = []
    cleared: list[tuple[str, int, str]] = []

    async def fake_build_profile(entity_id: uuid.UUID, **kwargs: object) -> None:
        built.append(entity_id)

    @asynccontextmanager
    async def fake_acting_as(principal_id: uuid.UUID) -> AsyncIterator[None]:
        yield None

    async def fake_set_value(
        session: object,
        owner_id: uuid.UUID,
        kind: str,
        counter: int = 0,
        payload: object = None,
        ref: str = "global",
    ) -> None:
        cleared.append((kind, counter, ref))

    monkeypatch.setattr(queue_mod, "build_profile", fake_build_profile)
    monkeypatch.setattr(queue_mod, "acting_as", fake_acting_as)
    monkeypatch.setattr(queue_mod.Watermark, "set_value", fake_set_value)

    asyncio.run(process_profile(entity, principal))

    assert built == [entity]
    assert cleared == [(Watermark.Kind.entity_dirty, 0, str(entity))]


@pytest.mark.parametrize("install_error", [None, DuplicateObjectError, DuplicateTableError])
def test_install_queue_schema_grants_every_table_and_sequence(
    monkeypatch: pytest.MonkeyPatch, install_error: type[Exception] | None
) -> None:
    """Installing the queue grants the app role each table and sequence, re-install tolerated."""
    grants: list[str] = []

    async def execute(sql: str) -> None:
        grants.append(sql)

    async def close() -> None:
        return None

    async def connect(dsn: str) -> SimpleNamespace:
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


def test_queue_connection_opens_and_closes_exactly_once(
    monkeypatch: pytest.MonkeyPatch, queue_seam: Callable[[object], object]
) -> None:
    """The shared connection scope opens once on entry and closes once on exit."""
    recorder = queue_seam(queue_mod)

    async def drive() -> None:
        async with queue_mod.queue_connection():
            assert recorder.opened == 1
            assert recorder.closed == 0

    asyncio.run(drive())
    assert recorder.opened == recorder.closed == 1


def test_enqueue_pending_skips_jobs_already_queued(
    monkeypatch: pytest.MonkeyPatch, queue_seam
) -> None:
    """Re-enqueuing pending chunks skips the ones still queued, the harmless-re-enqueue contract.

    The recorder raises `DuplicateJobError` on a repeated dedupe key the way pgqueuer does, so a
    second pass over the same pending chunk must swallow the duplicate and report only the fresh
    enqueues rather than crashing the promotion or fan-out path that called it.
    """
    recorder = queue_seam(queue_mod)
    principal = uuid.uuid4()
    chunk = SimpleNamespace(id=uuid.uuid4())

    async def fake_pending(principal_id: uuid.UUID, limit: int | None, source: str | None):
        return [chunk]

    monkeypatch.setattr(queue_mod, "pending_chunks", fake_pending)

    assert asyncio.run(queue_mod.enqueue_pending(principal_id=principal)) == 1
    assert asyncio.run(queue_mod.enqueue_pending(principal_id=principal)) == 0
    assert len(recorder.enqueues) == 1
