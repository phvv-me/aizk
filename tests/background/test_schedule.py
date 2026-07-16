import asyncio
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from typing import cast

import dbutil
import pytest
from bg_doubles import FakeJob, RecordingPg, RecordingQueue
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5, uuid5s
from pgqueuer import PgQueuer
from pydantic import UUID5

import aizk.background.schedule as schedule_mod
from aizk.background.jobs.maintenance import BackupJob, ProfileProjectionJob, ScheduledJob
from aizk.background.jobs.models import MaintenanceJob
from aizk.background.jobs.projection import ChunkProjectionJob
from aizk.background.schedule import fan_out, run_worker, scope_roster
from aizk.config import settings
from aizk.store import SessionItem
from aizk.types import Scopes

InstallSeam = Callable[[ModuleType], RecordingQueue]
# Per-scope tasks that fan out through the queue
fanned_job_classes = sorted(
    (cls for cls in ScheduledJob.implementations() if cls is not BackupJob),
    key=lambda cls: cls.name,
)
job_classes = st.sampled_from(fanned_job_classes)
crons = st.sampled_from(["0 3 * * *", "30 4 * * 0", "*/15 * * * *"])


@given(job_type=job_classes, users=st.lists(uuid5s, max_size=6, unique=True))
def test_fan_out_enqueues_one_deduped_job_per_user(
    monkeypatch: pytest.MonkeyPatch,
    queue_seam: InstallSeam,
    job_type: type[ScheduledJob],
    users: list[UUID5],
) -> None:
    recorder = queue_seam(schedule_mod)
    roster_reads: list[None] = []

    async def fake_scope_roster() -> list[Scopes]:
        roster_reads.append(None)
        return [frozenset({user}) for user in users]

    monkeypatch.setattr(schedule_mod, "scope_roster", fake_scope_roster)
    job = job_type()

    asyncio.run(fan_out(job))
    asyncio.run(fan_out(job))

    assert roster_reads == [None, None]
    assert len(recorder.enqueues) == len(users)
    for call, pid in zip(recorder.enqueues, users, strict=True):
        assert call.entrypoint == job.entrypoint
        assert call.priority == job.priority
        assert call.dedupe_key == f"{job.name}:{pid}"
        assert MaintenanceJob.decode(call.payload).scopes == frozenset({pid})
    assert recorder.opened == recorder.closed == 2


@given(job_type=job_classes, expression=crons, enabled=st.booleans())
def test_register_wires_the_queue_always_and_the_cron_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    pg_factory: type[RecordingPg],
    job_factory: type[FakeJob],
    job_type: type[ScheduledJob],
    expression: str,
    enabled: bool,
) -> None:
    pg = pg_factory()
    body_calls: list[Scopes] = []

    async def record_body(self: ScheduledJob, scopes: Scopes) -> None:
        body_calls.append(scopes)

    monkeypatch.setattr(job_type, "execute", record_body)
    fanned: list[ScheduledJob] = []

    async def fake_fan_out(job: ScheduledJob) -> None:
        fanned.append(job)

    monkeypatch.setattr(settings, f"{job_type.name}_cron", expression)
    monkeypatch.setattr(settings, f"{job_type.name}_enabled", enabled)
    job = job_type()
    job.register(cast(PgQueuer, pg), fake_fan_out)

    assert job.entrypoint in pg.entrypoints
    cron_registered = [entry for entry in pg.schedules if entry[0] == job.cron_entrypoint]
    assert bool(cron_registered) is enabled

    user = uuid5()
    key = frozenset({user})
    queued = job_factory(MaintenanceJob(scopes=key).encode())
    asyncio.run(pg.entrypoints[job.entrypoint](queued))
    assert body_calls == [key]

    if enabled:
        (_, registered_expression, fire) = cron_registered[0]
        assert registered_expression == expression
        asyncio.run(fire(SimpleNamespace()))
        assert fanned == [job]


def test_run_worker_registers_every_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    pg_factory: type[RecordingPg],
    queue_seam: Callable[[ModuleType], RecordingQueue],
) -> None:
    pg = pg_factory()
    recorder = queue_seam(schedule_mod)
    recorder.worker_instance = pg

    asyncio.run(run_worker(batch_size=7))

    chunk_entrypoint = ChunkProjectionJob().entrypoint
    profile_entrypoint = ProfileProjectionJob().entrypoint
    assert {chunk_entrypoint, profile_entrypoint} <= set(pg.entrypoints)
    assert pg.failure_policies[chunk_entrypoint] == "hold"
    assert pg.failure_policies[profile_entrypoint] == "hold"
    assert pg.concurrency_limits[chunk_entrypoint] == settings.graph_build_concurrency
    assert pg.concurrency_limits[profile_entrypoint] == 1
    for job_type in ScheduledJob.implementations():
        job = job_type()
        assert (job.entrypoint in pg.entrypoints) is (job_type is not BackupJob)
        registered = any(entry[0] == job.cron_entrypoint for entry in pg.schedules)
        assert registered is job.enabled
    assert pg.runs == [7]
    assert recorder.opened == recorder.closed == 1


def test_scope_roster_unions_document_and_session_scopes(migrated_db: None) -> None:
    doc_owner, session_owner = uuid5(), uuid5()

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
