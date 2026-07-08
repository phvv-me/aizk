import asyncio
import uuid
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from bg_doubles import FakeJob, RecordingPg, RecordingQueue
from hypothesis import given
from hypothesis import strategies as st

import aizk.background.queue as queue_mod
import aizk.background.schedule as schedule_mod
from aizk.background.payloads import ChunkJob, ProfileJob, TaskJob
from aizk.background.queue import EXTRACT_ENTRYPOINT, PROFILE_ENTRYPOINT
from aizk.background.schedule import fan_out, run_worker
from aizk.background.tasks import BackupTask, ScheduledTask
from aizk.config import settings

InstallSeam = Callable[[object], RecordingQueue]
# the per-user, fanned-out passes, excluding BackupTask, whose system-level cron-only shape
# (no per-user queue entrypoint, never fanned) is covered on its own in test_tasks.py.
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
    """The RLS-safe fan-out reads the roster once then queues exactly one job per user.

    The load-bearing no-leak boundary: each job targets the task's own entrypoint, carries one
    user's id, and is deduped on the task-and-user pair, so a fire that lands while the
    last is still draining re-enqueues nothing and no pass writes a second user's rows.
    """
    # the enqueue seam (AsyncpgDriver/Queries/asyncpg) now lives entirely in queue.queue_queries,
    # which fan_out drains through, so the recorder is installed on the queue module, not schedule
    recorder = queue_seam(queue_mod)
    roster_reads: list[None] = []

    async def fake_list_all(session: object) -> list[SimpleNamespace]:
        roster_reads.append(None)
        return [SimpleNamespace(id=pid) for pid in users]

    class FakeSystemSession:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(schedule_mod, "system_session", lambda: FakeSystemSession())
    monkeypatch.setattr(schedule_mod.User, "list_all", fake_list_all)
    task = task_cls()

    asyncio.run(fan_out(task))
    asyncio.run(fan_out(task))  # a fire while the last is still draining re-enqueues nothing

    assert roster_reads == [None, None]
    assert len(recorder.enqueues) == len(users)
    for call, pid in zip(recorder.enqueues, users, strict=True):
        assert call.entrypoint == task.queue_entrypoint
        assert call.dedupe_key == f"{task.name}:{pid}"
        assert TaskJob.decode(call.payload).user_id == pid
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
    """The queue body registers regardless of cadence, the cron fan-out only when the task is on.

    Drives both registered bodies too: the queue body runs the per-user pass under the
    user its payload names, and, when enabled, the cron body fires the fan-out for this task
    on exactly the crontab expression the task read off settings.
    """
    pg = pg_factory()
    body_calls: list[uuid.UUID] = []

    async def record_body(self: ScheduledTask, user_id: uuid.UUID) -> None:
        body_calls.append(user_id)

    monkeypatch.setattr(task_cls, "run", record_body)
    fanned: list[ScheduledTask] = []

    async def fake_fan_out(task: ScheduledTask) -> None:
        fanned.append(task)

    monkeypatch.setattr(settings, f"{task_cls.name}_cron", expression)
    monkeypatch.setattr(settings, f"{task_cls.name}_enabled", enabled)
    task = task_cls()
    task.register(pg, fake_fan_out)

    assert task.queue_entrypoint in pg.entrypoints
    cron_registered = [entry for entry in pg.schedules if entry[0] == task.cron_entrypoint]
    assert bool(cron_registered) is enabled

    user = uuid.uuid4()
    job = job_factory(TaskJob(user_id=user).encode())
    asyncio.run(pg.entrypoints[task.queue_entrypoint](job))
    assert body_calls == [user]

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
    """The worker wires the on-write chain and every scheduled pass, chaining profiles only if on.

    The single worker registers the extract and profile entrypoints plus each task's queue and cron
    bodies, starts both loops with the batch size, and closes its connection. Driving the extract
    body proves the build chains a debounced profile rebuild under profile-on-write and never with
    it off, and the profile body rebuilds exactly the entity its payload names.
    """
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
    process_calls: list[tuple[uuid.UUID, uuid.UUID]] = []
    profile_jobs: list[list[uuid.UUID]] = []
    profile_calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def fake_process_chunk(chunk_id: uuid.UUID, user_id: uuid.UUID) -> list[uuid.UUID]:
        process_calls.append((chunk_id, user_id))
        return [entity]

    async def fake_enqueue_profiles(entity_ids: list[uuid.UUID], user_id: uuid.UUID) -> None:
        profile_jobs.append(list(entity_ids))

    async def fake_process_profile(entity_id: uuid.UUID, user_id: uuid.UUID) -> None:
        profile_calls.append((entity_id, user_id))

    monkeypatch.setattr(schedule_mod, "process_chunk", fake_process_chunk)
    monkeypatch.setattr(schedule_mod, "enqueue_profiles", fake_enqueue_profiles)
    monkeypatch.setattr(schedule_mod, "process_profile", fake_process_profile)
    monkeypatch.setattr(settings, "profile_on_write", profile_on_write)

    asyncio.run(run_worker(batch_size=7))

    assert {EXTRACT_ENTRYPOINT, PROFILE_ENTRYPOINT} <= set(pg.entrypoints)
    for task_cls in ScheduledTask.implementations():
        task = task_cls()
        # the fanned passes register a per-user queue entrypoint always; BackupTask, the one
        # system-level pass, registers only its cron, never a per-user entrypoint.
        assert (task.queue_entrypoint in pg.entrypoints) is (task_cls is not BackupTask)
        registered = any(entry[0] == task.cron_entrypoint for entry in pg.schedules)
        assert registered is task.enabled
    assert pg.runs == [7]
    assert closed == [True]

    chunk_job = job_factory(ChunkJob(chunk_id=chunk, user_id=user).encode())
    asyncio.run(pg.entrypoints[EXTRACT_ENTRYPOINT](chunk_job))
    assert process_calls == [(chunk, user)]
    assert profile_jobs == ([[entity]] if profile_on_write else [])

    profile_job = job_factory(ProfileJob(entity_id=entity, user_id=user).encode())
    asyncio.run(pg.entrypoints[PROFILE_ENTRYPOINT](profile_job))
    assert profile_calls == [(entity, user)]
