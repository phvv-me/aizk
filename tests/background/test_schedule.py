import asyncio
import uuid
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

import aizk.background.queue as queue_mod
import aizk.background.schedule as schedule_mod
from aizk.background.payloads import ChunkJob, ProfileJob, TaskJob
from aizk.background.queue import EXTRACT_ENTRYPOINT, PROFILE_ENTRYPOINT
from aizk.background.schedule import fan_out, run_worker
from aizk.background.tasks import ScheduledTask
from aizk.config import settings

task_classes = st.sampled_from(sorted(ScheduledTask.implementations(), key=lambda cls: cls.name))
crons = st.sampled_from(["0 3 * * *", "30 4 * * 0", "*/15 * * * *"])


def test_entrypoint_names_are_prefixed_and_injective() -> None:
    """Each registered task mints a distinct queue and cron entrypoint, none colliding."""
    tasks = [task_cls() for task_cls in ScheduledTask.implementations()]
    for task in tasks:
        assert task.queue_entrypoint.startswith("aizk_task_")
        assert task.cron_entrypoint.startswith("aizk_cron_")
        assert task.queue_entrypoint != task.cron_entrypoint
    queue_names = [task.queue_entrypoint for task in tasks]
    cron_names = [task.cron_entrypoint for task in tasks]
    assert len(queue_names) == len(set(queue_names))
    assert len(cron_names) == len(set(cron_names))


@given(task_cls=task_classes, principals=st.lists(st.uuids(), max_size=6, unique=True))
def test_fan_out_enqueues_one_deduped_job_per_principal(
    monkeypatch: pytest.MonkeyPatch, queue_seam, task_cls: type[ScheduledTask], principals
) -> None:
    """The RLS-safe fan-out reads the roster once then queues exactly one job per principal.

    The load-bearing no-leak boundary: each job targets the task's own entrypoint, carries one
    principal's id, and is deduped on the task-and-principal pair, so no pass ever writes a second
    principal's rows in the same transaction.
    """
    recorder = queue_seam(schedule_mod)
    roster_reads: list[None] = []

    async def fake_list_all(session: object) -> list[SimpleNamespace]:
        roster_reads.append(None)
        return [SimpleNamespace(id=pid) for pid in principals]

    class FakeSystemSession:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(schedule_mod, "system_session", lambda: FakeSystemSession())
    monkeypatch.setattr(schedule_mod.Principal, "list_all", fake_list_all)
    task = task_cls()

    asyncio.run(fan_out(task))
    asyncio.run(fan_out(task))  # a fire while the last is still draining re-enqueues nothing

    assert roster_reads == [None, None]
    assert len(recorder.enqueues) == len(principals)
    for call, pid in zip(recorder.enqueues, principals, strict=True):
        assert call.entrypoint == task.queue_entrypoint
        assert call.dedupe_key == f"{task.name}:{pid}"
        assert TaskJob.decode(call.payload).principal_id == pid
    assert recorder.opened == recorder.closed == 2


@given(task_cls=task_classes, expression=crons, enabled=st.booleans())
def test_register_wires_the_queue_always_and_the_cron_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    pg_factory,
    job_factory,
    task_cls: type[ScheduledTask],
    expression: str,
    enabled: bool,
) -> None:
    """The queue body registers regardless of cadence, the cron fan-out only when the task is on.

    Also drives the two registered bodies: the queue body runs the per-principal pass under the
    principal its payload names, and the cron body fans the task out across the roster.
    """
    pg = pg_factory()
    body_calls: list[uuid.UUID] = []

    async def record_body(self: ScheduledTask, principal_id: uuid.UUID) -> None:
        body_calls.append(principal_id)

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

    principal = uuid.uuid4()
    job = job_factory(TaskJob(principal_id=principal).encode())
    asyncio.run(pg.entrypoints[task.queue_entrypoint](job))
    assert body_calls == [principal]

    if enabled:
        (_, registered_expression, fire) = cron_registered[0]
        assert registered_expression == expression
        asyncio.run(fire(SimpleNamespace()))
        assert fanned == [task]


@pytest.mark.parametrize("profile_on_write", [True, False], ids=["chained", "skipped"])
def test_run_worker_registers_every_entrypoint_and_chains_profiles_only_when_on(
    monkeypatch: pytest.MonkeyPatch, pg_factory, job_factory, profile_on_write: bool
) -> None:
    """The worker wires the on-write chain and every scheduled pass, chaining profiles only if on.

    The single worker registers the extract and profile entrypoints plus each task's queue and cron
    bodies, starts both loops, and closes its connection. Driving the extract body then proves the
    build slice chains a debounced profile rebuild under profile-on-write and never with it off,
    and the profile body rebuilds the named entity when the chain is live.
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

    chunk, principal, entity = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    process_calls: list[tuple[uuid.UUID, uuid.UUID]] = []
    profile_jobs: list[list[uuid.UUID]] = []
    profile_calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def fake_process_chunk(chunk_id: uuid.UUID, principal_id: uuid.UUID) -> list[uuid.UUID]:
        process_calls.append((chunk_id, principal_id))
        return [entity]

    async def fake_enqueue_profiles(entity_ids: list[uuid.UUID], principal_id: uuid.UUID) -> None:
        profile_jobs.append(list(entity_ids))

    async def fake_process_profile(entity_id: uuid.UUID, principal_id: uuid.UUID) -> None:
        profile_calls.append((entity_id, principal_id))

    monkeypatch.setattr(schedule_mod, "process_chunk", fake_process_chunk)
    monkeypatch.setattr(schedule_mod, "enqueue_profiles", fake_enqueue_profiles)
    monkeypatch.setattr(schedule_mod, "process_profile", fake_process_profile)

    monkeypatch.setattr(settings, "profile_on_write", profile_on_write)
    asyncio.run(run_worker(batch_size=7))

    assert {EXTRACT_ENTRYPOINT, PROFILE_ENTRYPOINT} <= set(pg.entrypoints)
    for task_cls in ScheduledTask.implementations():
        task = task_cls()
        assert task.queue_entrypoint in pg.entrypoints
        registered = any(entry[0] == task.cron_entrypoint for entry in pg.schedules)
        assert registered is task.enabled
    assert pg.runs == [7]
    assert closed == [True]

    chunk_job = job_factory(ChunkJob(chunk_id=chunk, principal_id=principal).encode())
    asyncio.run(pg.entrypoints[EXTRACT_ENTRYPOINT](chunk_job))
    assert process_calls == [(chunk, principal)]
    assert profile_jobs == ([[entity]] if profile_on_write else [])

    profile_job = job_factory(ProfileJob(entity_id=entity, principal_id=principal).encode())
    asyncio.run(pg.entrypoints[PROFILE_ENTRYPOINT](profile_job))
    assert profile_calls == [(entity, principal)]
