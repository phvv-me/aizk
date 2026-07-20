import asyncio
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest
from bg_doubles import RecordingPg, RecordingQueue, fake_artifact_services, fake_runtime
from doubles import AsyncContext
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5
from pgqueuer import PgQueuer
from pgqueuer.models import Schedule
from sqlmodel.sql.expression import Select

import aizk.background.jobs.maintenance as jobs_mod
from aizk.artifacts.service import ArtifactIntake, ArtifactIntegrity
from aizk.background.jobs.maintenance import (
    ArtifactDispatchJob,
    ArtifactIntegrityJob,
    BackupJob,
    ChunkDispatchJob,
    ChunkRecoveryJob,
    CommunitiesJob,
    DecayJob,
    DedupJob,
    InsightJob,
    ProfileProjectionJob,
    ProfileRefreshJob,
    RaptorJob,
    ScheduledJob,
    ScopedScheduledJob,
    SessionPromoteJob,
    SystemScheduledJob,
    retry_failed_profile_projections,
)
from aizk.config import settings
from aizk.store import Watermark
from aizk.store.identity import User
from aizk.types import Scopes


@given(
    flags=st.lists(st.booleans(), min_size=13, max_size=13),
    cron=st.sampled_from(["0 3 * * *", "30 4 * * 0"]),
)
def test_job_registry_names_and_settings_are_coherent(
    flags: list[bool], cron: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = [job_type.assemble(fake_runtime()) for job_type in ScheduledJob.implementations()]
    assert {job.name for job in jobs} == {
        "decay",
        "dedup",
        "communities",
        "raptor",
        "profile_projection",
        "profile_refresh",
        "session_promote",
        "insight",
        "backup",
        "artifact_dispatch",
        "chunk_dispatch",
        "chunk_recovery",
        "artifact_integrity",
    }
    scoped = [job for job in jobs if isinstance(job, ScopedScheduledJob)]
    queue_names = {job.entrypoint for job in scoped}
    cron_names = {job.cron_entrypoint for job in jobs}
    assert queue_names == {f"aizk_task_{job.name}" for job in scoped}
    assert cron_names == {f"aizk_cron_{job.name}" for job in jobs}
    assert queue_names.isdisjoint(cron_names)
    overrides: dict[str, bool | str] = {
        f"{job.name}_enabled": flag for job, flag in zip(jobs, flags, strict=True)
    }
    overrides["decay_cron"] = cron
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    for job in jobs:
        assert job.enabled is getattr(settings, f"{job.name}_enabled")
        assert job.expression == getattr(settings, f"{job.name}_cron")


@pytest.mark.parametrize(
    ("job_type", "target"),
    [
        (DecayJob, "decay"),
        (DedupJob, "dedup_entities"),
        (ProfileProjectionJob, "refresh_dirty_profiles"),
        (ProfileRefreshJob, "refresh_profiles"),
        (SessionPromoteJob, "promote_sessions"),
        (InsightJob, "derive_insights"),
    ],
)
def test_thin_passes_delegate_under_the_user(
    monkeypatch: pytest.MonkeyPatch, job_type: type[ScopedScheduledJob], target: str
) -> None:
    user = uuid5()
    seen: list[Scopes] = []

    async def record(*args: object, scopes: Scopes, **kwargs: float) -> None:
        seen.append(scopes)

    monkeypatch.setattr(jobs_mod, target, record)
    asyncio.run(job_type.assemble(fake_runtime()).execute(frozenset({user})))
    assert seen == [frozenset({user})]


def test_artifact_dispatch_recovers_pending_originals() -> None:
    scopes = frozenset({uuid5()})
    calls: list[Scopes] = []

    class Intake:
        async def dispatch_pending(self, target: Scopes) -> int:
            calls.append(target)
            return 2

    services = fake_artifact_services(intake=cast("ArtifactIntake", Intake()))

    asyncio.run(ArtifactDispatchJob.assemble(fake_runtime(artifacts=services)).execute(scopes))

    assert calls == [scopes]


def test_chunk_dispatch_recovers_pending_graph_work(monkeypatch: pytest.MonkeyPatch) -> None:
    scopes = frozenset({uuid5()})
    calls: list[tuple[int, Scopes]] = []

    async def enqueue(limit: int, target: Scopes) -> int:
        calls.append((limit, target))
        return 3

    monkeypatch.setattr(jobs_mod, "enqueue_pending", enqueue)
    asyncio.run(ChunkDispatchJob().execute(scopes))

    assert calls == [(settings.chunk_dispatch_batch_size, scopes)]


def test_chunk_recovery_bounds_automatic_retry_cycles(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int | None]] = []

    async def retry(limit: int, max_cycles: int | None) -> int:
        calls.append((limit, max_cycles))
        return 3

    monkeypatch.setattr(jobs_mod, "retry_failed_chunks", retry)
    asyncio.run(ChunkRecoveryJob().execute())

    assert calls == [(settings.chunk_recovery_batch_size, settings.chunk_recovery_max_cycles)]


def test_profile_projection_recovery_uses_its_typed_queue_boundary(
    queue_seam: Callable[[ModuleType], RecordingQueue],
) -> None:
    recorder = queue_seam(jobs_mod)

    assert asyncio.run(retry_failed_profile_projections(limit=7)) == 4
    assert recorder.failed_requeues == [(ProfileProjectionJob.entrypoint, 7)]
    assert recorder.opened == recorder.closed == 1


def test_artifact_integrity_runs_as_one_system_cron(monkeypatch: pytest.MonkeyPatch) -> None:
    reports: list[tuple[int, int]] = []

    class Integrity:
        async def verify(self, limit: int, interval_days: int) -> SimpleNamespace:
            reports.append((limit, interval_days))
            return SimpleNamespace(checked=3, valid=2, failed=1)

    services = fake_artifact_services(integrity=cast("ArtifactIntegrity", Integrity()))
    runtime = fake_runtime(artifacts=services)
    asyncio.run(ArtifactIntegrityJob.assemble(runtime).fire_cron(cast(Schedule, None)))

    assert reports == [
        (settings.artifact_integrity_batch_size, settings.artifact_integrity_interval_days)
    ]


class GateSession:
    def __init__(self, current: int) -> None:
        self.current = current

    async def exec(self, statement: Select) -> GateSession:
        return self

    def one(self) -> int:
        return self.current


@pytest.mark.parametrize(
    ("job_type", "threshold_field", "kind", "build_target"),
    [
        (
            CommunitiesJob,
            "communities_every_n_facts",
            Watermark.Kind.fact_count,
            "build_communities",
        ),
        (RaptorJob, "raptor_every_n_facts", Watermark.Kind.raptor_fact_count, "build_raptor"),
    ],
)
@given(current=st.integers(0, 400), last=st.integers(0, 400), threshold=st.integers(1, 200))
def test_growth_gated_passes_build_only_past_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
    job_type: type[ScopedScheduledJob],
    threshold_field: str,
    kind: Watermark.Kind,
    build_target: str,
    current: int,
    last: int,
    threshold: int,
) -> None:
    user = uuid5()
    builds: list[Scopes] = []
    set_calls: list[tuple[Watermark.Kind, int]] = []

    def fake_transaction(user: User) -> AsyncContext[GateSession]:
        return AsyncContext(GateSession(current))

    async def fake_read(
        session: GateSession, scopes: Scopes, watermark_kind: Watermark.Kind
    ) -> int:
        return last

    async def fake_set_value(
        session: GateSession,
        scopes: Scopes,
        watermark_kind: Watermark.Kind,
        counter: int = 0,
    ) -> None:
        set_calls.append((watermark_kind, counter))

    async def fake_build(*args: object, scopes: Scopes) -> None:
        builds.append(scopes)

    monkeypatch.setattr(User, "app", property(fake_transaction))
    monkeypatch.setattr(jobs_mod.Watermark, "read", fake_read)
    monkeypatch.setattr(jobs_mod.Watermark, "set_value", fake_set_value)
    monkeypatch.setattr(jobs_mod, build_target, fake_build)
    monkeypatch.setattr(settings, threshold_field, threshold)

    key = frozenset({user})
    asyncio.run(job_type.assemble(fake_runtime()).execute(key))

    should_build = current - last >= threshold
    assert (builds == [key]) is should_build
    assert set_calls == ([(kind, current)] if should_build else [])


def test_backup_job_fire_cron_runs_the_scheduled_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[bool] = []

    async def fake_scheduled_backup() -> SimpleNamespace:
        ran.append(True)
        return SimpleNamespace(bytes=7, path="/backups/x.dump")

    monkeypatch.setattr(jobs_mod, "scheduled_backup", fake_scheduled_backup)
    asyncio.run(BackupJob().fire_cron(cast(Schedule, None)))
    assert ran == [True]


@pytest.mark.parametrize("job_type", [BackupJob, ArtifactIntegrityJob, ChunkRecoveryJob])
@pytest.mark.parametrize("enabled", [True, False])
def test_system_jobs_register_only_the_cron_and_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch, job_type: type[SystemScheduledJob], enabled: bool
) -> None:
    monkeypatch.setattr(settings, f"{job_type.name}_enabled", enabled)
    monkeypatch.setattr(settings, f"{job_type.name}_cron", "0 2 * * *")
    pg = RecordingPg()

    async def unused_fan_out(job: ScopedScheduledJob) -> None:
        pass

    job_type.assemble(fake_runtime()).register(cast(PgQueuer, pg), fan_out=unused_fan_out)

    assert pg.entrypoints == {}  # never a per-user entrypoint, whatever the flag
    assert len(pg.schedules) == (1 if enabled else 0)
    if enabled:
        assert pg.schedules[0][:2] == (f"aizk_cron_{job_type.name}", "0 2 * * *")
