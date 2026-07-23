import asyncio
from collections.abc import Callable
from dataclasses import replace
from types import ModuleType, SimpleNamespace
from typing import cast

import dbutil
import pytest
from bg_doubles import FakeJob, RecordingPg, RecordingQueue, fake_runtime
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5, uuid5s
from pgqueuer import PgQueuer
from pydantic import UUID5

import aizk.background.schedule as schedule_mod
from aizk.background.jobs.conversion import ArtifactProcessor, DoclingConversionJob
from aizk.background.jobs.maintenance import (
    ProfileProjectionJob,
    ScheduledJob,
    ScopedScheduledJob,
)
from aizk.background.jobs.models import MaintenanceJob
from aizk.background.jobs.projection import ChunkProjectionJob
from aizk.background.portable import PortableWorker
from aizk.background.schedule import (
    fan_out,
    portable_worker,
    run_worker,
    run_worker_once,
    scope_roster,
)
from aizk.config import DatabaseBackend, settings
from aizk.ontology import Ontology
from aizk.runtime import Runtime
from aizk.store import SessionItem
from aizk.store.engine import Session
from aizk.types import Scopes
from aizk.usage import UsageAccountingJob

InstallSeam = Callable[[ModuleType], RecordingQueue]
# Per-scope tasks that fan out through the queue
fanned_job_classes = sorted(
    ScopedScheduledJob.implementations(),
    key=lambda cls: cls.name,
)
job_classes = st.sampled_from(fanned_job_classes)
crons = st.sampled_from(["0 3 * * *", "30 4 * * 0", "*/15 * * * *"])


@given(job_type=job_classes, users=st.lists(uuid5s, max_size=6, unique=True))
def test_fan_out_enqueues_one_deduped_job_per_user(
    monkeypatch: pytest.MonkeyPatch,
    queue_seam: InstallSeam,
    job_type: type[ScopedScheduledJob],
    users: list[UUID5],
) -> None:
    recorder = queue_seam(schedule_mod)
    roster_reads: list[None] = []

    async def fake_scope_roster() -> list[Scopes]:
        roster_reads.append(None)
        return [frozenset({user}) for user in users]

    monkeypatch.setattr(schedule_mod, "scope_roster", fake_scope_roster)
    job = job_type.assemble(fake_runtime())

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
    job_type: type[ScopedScheduledJob],
    expression: str,
    enabled: bool,
) -> None:
    pg = pg_factory()
    body_calls: list[Scopes] = []

    async def record_body(self: ScopedScheduledJob, scopes: Scopes) -> None:
        body_calls.append(scopes)

    monkeypatch.setattr(job_type, "execute", record_body)
    fanned: list[ScopedScheduledJob] = []

    async def fake_fan_out(job: ScopedScheduledJob) -> None:
        fanned.append(job)

    monkeypatch.setattr(settings, f"{job_type.name}_cron", expression)
    monkeypatch.setattr(settings, f"{job_type.name}_enabled", enabled)
    job = job_type.assemble(fake_runtime())
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

    runtime = fake_runtime()
    asyncio.run(run_worker(runtime, batch_size=7))
    asyncio.run(run_worker(runtime))

    chunk_entrypoint = ChunkProjectionJob.entrypoint
    profile_entrypoint = ProfileProjectionJob.entrypoint
    usage_entrypoint = UsageAccountingJob().entrypoint
    assert {chunk_entrypoint, profile_entrypoint, usage_entrypoint} <= set(pg.entrypoints)
    assert pg.failure_policies[usage_entrypoint] == "hold"
    assert pg.failure_policies[chunk_entrypoint] == "hold"
    assert pg.failure_policies[profile_entrypoint] == "hold"
    assert pg.concurrency_limits[chunk_entrypoint] == settings.graph_build_concurrency
    assert pg.concurrency_limits[profile_entrypoint] == 1
    for job_type in ScheduledJob.implementations():
        job = job_type.assemble(runtime)
        if isinstance(job, ScopedScheduledJob):
            assert job.entrypoint in pg.entrypoints
        registered = any(entry[0] == job.cron_entrypoint for entry in pg.schedules)
        assert registered is job.enabled
    assert pg.runs == [7, settings.queue_batch_size]
    assert recorder.opened == recorder.closed == 2


def test_portable_worker_assembles_every_enabled_job_and_schedule() -> None:
    runtime = fake_runtime()
    conversion = DoclingConversionJob(cast(ArtifactProcessor, SimpleNamespace()))
    services = replace(runtime.artifacts, conversion=conversion)
    runtime = replace(runtime, artifacts=services)

    worker = portable_worker(runtime, batch_size=3)

    assert worker.batch_size == 3
    assert {
        ChunkProjectionJob.entrypoint,
        UsageAccountingJob.entrypoint,
        DoclingConversionJob.entrypoint,
    } <= set(worker.jobs)
    expected_schedules = {
        job.cron_entrypoint
        for job_type in ScheduledJob.implementations()
        if (job := job_type.assemble(runtime)).enabled
    }
    assert set(worker.schedules) == expected_schedules


class PortableRunner:
    """Record serverless and continuous portable worker calls."""

    def __init__(self) -> None:
        self.installed = 0
        self.once = 0
        self.runs = 0

    async def install_schedules(self) -> None:
        self.installed += 1

    async def run_once(self) -> int:
        self.once += 1
        return 6

    async def run(self) -> None:
        self.runs += 1


def test_serverless_worker_requires_cockroach_and_drains_one_wave(
    migrated_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = fake_runtime()
    runner = PortableRunner()
    refreshed: list[Session] = []

    async def refresh(cls: type[Ontology], session: Session) -> None:
        del cls
        refreshed.append(session)

    def assemble(received: Runtime, batch_size: int | None = None) -> PortableWorker:
        assert received is runtime
        assert batch_size == 2
        return cast(PortableWorker, runner)

    monkeypatch.setattr(Ontology, "refresh", classmethod(refresh))
    monkeypatch.setattr(schedule_mod, "portable_worker", assemble)
    monkeypatch.setattr(settings, "database_backend", DatabaseBackend.postgresql)
    with pytest.raises(RuntimeError, match="CockroachDB"):
        asyncio.run(run_worker_once(runtime, 2))

    monkeypatch.setattr(settings, "database_backend", DatabaseBackend.cockroachdb)
    assert asyncio.run(run_worker_once(runtime, 2)) == 6
    asyncio.run(run_worker(runtime, 2))

    assert runner.installed == 1
    assert runner.once == 1
    assert runner.runs == 1
    assert len(refreshed) == 2


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
