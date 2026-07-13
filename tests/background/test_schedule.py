import asyncio
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import dbutil
import pytest
from bg_doubles import FakeJob, RecordingPg, RecordingQueue
from hypothesis import given
from hypothesis import strategies as st
from pgqueuer import PgQueuer

import aizk.background.queue as queue_mod
import aizk.background.schedule as schedule_mod
from aizk.background.payloads import ChunkJob, ProfileJob, TaskJob
from aizk.background.queue import EXTRACT_ENTRYPOINT, PROFILE_ENTRYPOINT
from aizk.background.schedule import fan_out, run_worker, scope_roster
from aizk.background.tasks import BackupTask, ScheduledTask
from aizk.config import settings
from aizk.store import SessionItem
from aizk.types import Scopes

InstallSeam = Callable[[object], RecordingQueue]
# Per-scope tasks that fan out through the queue
fanned_task_classes = sorted(
    (cls for cls in ScheduledTask.implementations() if cls is not BackupTask),
    key=lambda cls: cls.name,
)
task_classes = st.sampled_from(fanned_task_classes)
crons = st.sampled_from(["0 3 * * *", "30 4 * * 0", "*/15 * * * *"])


@given(task_cls=task_classes, users=st.lists(st.uuids(), max_size=6, unique=True))
def test_fan_out_enqueues_one_deduped_job_per_user(
    monkeypatch: pytest.MonkeyPatch,
    queue_seam: InstallSeam,
    task_cls: type[ScheduledTask],
    users: list[uuid.UUID],
) -> None:
    recorder = queue_seam(queue_mod)
    roster_reads: list[None] = []

    async def fake_scope_roster() -> list[Scopes]:
        roster_reads.append(None)
        return [frozenset({user}) for user in users]

    monkeypatch.setattr(schedule_mod, "scope_roster", fake_scope_roster)
    task = task_cls()

    asyncio.run(fan_out(task))
    asyncio.run(fan_out(task))  # a fire while the last is still draining re-enqueues nothing

    assert roster_reads == [None, None]
    assert len(recorder.enqueues) == len(users)
    for call, pid in zip(recorder.enqueues, users, strict=True):
        assert call.entrypoint == task.queue_entrypoint
        assert call.dedupe_key == f"{task.name}:{pid}"
        assert TaskJob.decode(call.payload).scopes == frozenset({pid})
    assert recorder.opened == recorder.closed == 2


@given(task_cls=task_classes, expression=crons, enabled=st.booleans())
def test_register_wires_the_queue_always_and_the_cron_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    pg_factory: type[RecordingPg],
    job_factory: type[FakeJob],
    task_cls: type[ScheduledTask],
    expression: str,
    enabled: bool,
) -> None:
    pg = pg_factory()
    body_calls: list[Scopes] = []

    async def record_body(self: ScheduledTask, scopes: Scopes) -> None:
        body_calls.append(scopes)

    monkeypatch.setattr(task_cls, "run", record_body)
    fanned: list[ScheduledTask] = []

    async def fake_fan_out(task: ScheduledTask) -> None:
        fanned.append(task)

    monkeypatch.setattr(settings, f"{task_cls.name}_cron", expression)
    monkeypatch.setattr(settings, f"{task_cls.name}_enabled", enabled)
    task = task_cls()
    task.register(cast(PgQueuer, pg), fake_fan_out)

    assert task.queue_entrypoint in pg.entrypoints
    cron_registered = [entry for entry in pg.schedules if entry[0] == task.cron_entrypoint]
    assert bool(cron_registered) is enabled

    user = uuid.uuid4()
    key = frozenset({user})
    job = job_factory(TaskJob(scopes=key).encode())
    asyncio.run(pg.entrypoints[task.queue_entrypoint](job))
    assert body_calls == [key]

    if enabled:
        (_, registered_expression, fire) = cron_registered[0]
        assert registered_expression == expression
        asyncio.run(fire(SimpleNamespace()))
        assert fanned == [task]


@pytest.mark.parametrize("profile_on_write", [True, False], ids=["chained", "skipped"])
def test_run_worker_registers_every_entrypoint_and_chains_profiles_only_when_on(
    monkeypatch: pytest.MonkeyPatch,
    pg_factory: type[RecordingPg],
    job_factory: type[FakeJob],
    profile_on_write: bool,
) -> None:
    pg = pg_factory()
    closed: list[bool] = []

    async def fake_connect(dsn: str) -> SimpleNamespace:
        async def close() -> None:
            closed.append(True)

        return SimpleNamespace(close=close)

    monkeypatch.setattr(queue_mod, "asyncpg", SimpleNamespace(connect=fake_connect))
    monkeypatch.setattr(
        schedule_mod, "PgQueuer", SimpleNamespace(from_asyncpg_connection=lambda conn: pg)
    )

    chunk, user, entity = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    process_calls: list[tuple[uuid.UUID, Scopes]] = []
    profile_jobs: list[list[uuid.UUID]] = []
    profile_calls: list[tuple[uuid.UUID, Scopes]] = []

    async def fake_process_chunk(chunk_id: uuid.UUID, scopes: Scopes) -> list[uuid.UUID]:
        process_calls.append((chunk_id, scopes))
        return [entity]

    async def fake_enqueue_profiles(entity_ids: list[uuid.UUID], scopes: Scopes) -> None:
        profile_jobs.append(list(entity_ids))

    async def fake_process_profile(entity_id: uuid.UUID, scopes: Scopes) -> None:
        profile_calls.append((entity_id, scopes))

    monkeypatch.setattr(schedule_mod, "process_chunk", fake_process_chunk)
    monkeypatch.setattr(schedule_mod, "enqueue_profiles", fake_enqueue_profiles)
    monkeypatch.setattr(schedule_mod, "process_profile", fake_process_profile)
    monkeypatch.setattr(settings, "profile_on_write", profile_on_write)

    asyncio.run(run_worker(batch_size=7))

    assert {EXTRACT_ENTRYPOINT, PROFILE_ENTRYPOINT} <= set(pg.entrypoints)
    for task_cls in ScheduledTask.implementations():
        task = task_cls()
        # Backup is process-wide and therefore has no per-scope queue entrypoint.
        assert (task.queue_entrypoint in pg.entrypoints) is (task_cls is not BackupTask)
        registered = any(entry[0] == task.cron_entrypoint for entry in pg.schedules)
        assert registered is task.enabled
    assert pg.runs == [7]
    assert closed == [True]

    key = frozenset({user})
    chunk_job = job_factory(ChunkJob(chunk_id=chunk, scopes=key).encode())
    asyncio.run(pg.entrypoints[EXTRACT_ENTRYPOINT](chunk_job))
    assert process_calls == [(chunk, key)]
    assert profile_jobs == ([[entity]] if profile_on_write else [])

    profile_job = job_factory(ProfileJob(entity_id=entity, scopes=key).encode())
    asyncio.run(pg.entrypoints[PROFILE_ENTRYPOINT](profile_job))
    assert profile_calls == [(entity, key)]


def test_scope_roster_unions_document_and_session_scopes(migrated_db: None) -> None:
    doc_owner, session_owner = uuid.uuid4(), uuid.uuid4()

    async def body() -> set[Scopes]:
        await dbutil.reset_db()
        await dbutil.seed_document(doc_owner, [doc_owner])
        async with dbutil.actor(session_owner) as session:
            session.add(
                SessionItem(
                    text="a decision",
                    created_by=session_owner,
                    scopes=[session_owner],
                )
            )
        return set(await scope_roster())

    roster = dbutil.run(body())
    assert {frozenset({doc_owner}), frozenset({session_owner})} <= roster
