import asyncio
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast

import pytest
from bg_doubles import RecordingPg
from hypothesis import given
from hypothesis import strategies as st
from pgqueuer import PgQueuer
from pgqueuer.models import Schedule

import aizk.background.tasks as tasks_mod
from aizk.background.tasks import (
    BackupTask,
    CommunitiesTask,
    DecayTask,
    DedupTask,
    InsightTask,
    ProfileRefreshTask,
    RaptorTask,
    ScheduledTask,
    SelfImproveTask,
    SessionPromoteTask,
)
from aizk.config import settings
from aizk.eval import EvalReport
from aizk.store import Watermark, engine
from aizk.store.identity import User
from aizk.types import Scopes


@given(
    flags=st.lists(st.booleans(), min_size=9, max_size=9),
    cron=st.sampled_from(["0 3 * * *", "30 4 * * 0"]),
)
def test_task_registry_names_and_settings_are_coherent(
    flags: list[bool], cron: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = [task_cls() for task_cls in ScheduledTask.implementations()]
    assert {task.name for task in tasks} == {
        "decay",
        "dedup",
        "communities",
        "raptor",
        "profile_refresh",
        "self_improve",
        "session_promote",
        "insight",
        "backup",
    }
    queue_names = {task.queue_entrypoint for task in tasks}
    cron_names = {task.cron_entrypoint for task in tasks}
    assert queue_names == {f"aizk_task_{task.name}" for task in tasks}
    assert cron_names == {f"aizk_cron_{task.name}" for task in tasks}
    assert queue_names.isdisjoint(cron_names)
    overrides: dict[str, bool | str] = {
        f"{task.name}_enabled": flag for task, flag in zip(tasks, flags, strict=True)
    }
    overrides["decay_cron"] = cron
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    for task in tasks:
        assert task.enabled is getattr(settings, f"{task.name}_enabled")
        assert task.expression == getattr(settings, f"{task.name}_cron")


@pytest.mark.parametrize(
    ("task_cls", "target"),
    [
        (DecayTask, "decay"),
        (DedupTask, "dedup_entities"),
        (ProfileRefreshTask, "refresh_profiles"),
        (SessionPromoteTask, "promote_sessions"),
        (InsightTask, "derive_insights"),
    ],
)
def test_thin_passes_delegate_under_the_user(
    monkeypatch: pytest.MonkeyPatch, task_cls: type[ScheduledTask], target: str
) -> None:
    user = uuid.uuid4()
    seen: list[Scopes] = []

    async def record(*, scopes: Scopes, **kwargs: float) -> None:
        seen.append(scopes)

    monkeypatch.setattr(tasks_mod, target, record)
    asyncio.run(task_cls().run(frozenset({user})))
    assert seen == [frozenset({user})]


class GateSession:
    def __init__(self, current: int) -> None:
        self.current = current

    async def exec(self, statement: object) -> GateSession:
        return self

    def one(self) -> int:
        return self.current


@pytest.mark.parametrize(
    ("task_cls", "threshold_field", "kind", "build_target"),
    [
        (
            CommunitiesTask,
            "communities_every_n_facts",
            Watermark.Kind.fact_count,
            "build_communities",
        ),
        (RaptorTask, "raptor_every_n_facts", Watermark.Kind.raptor_fact_count, "build_raptor"),
    ],
)
@given(current=st.integers(0, 400), last=st.integers(0, 400), threshold=st.integers(1, 200))
def test_growth_gated_passes_build_only_past_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
    task_cls: type[ScheduledTask],
    threshold_field: str,
    kind: Watermark.Kind,
    build_target: str,
    current: int,
    last: int,
    threshold: int,
) -> None:
    user = uuid.uuid4()
    builds: list[Scopes] = []
    set_calls: list[tuple[Watermark.Kind, int]] = []

    @asynccontextmanager
    async def fake_transaction(user: User) -> AsyncGenerator[GateSession]:
        yield GateSession(current)

    async def fake_read(
        session: object, scopes: Scopes, watermark_kind: Watermark.Kind, **kw: object
    ) -> int:
        return last

    async def fake_set_value(
        session: object,
        scopes: Scopes,
        watermark_kind: Watermark.Kind,
        counter: int = 0,
        **kw: object,
    ) -> None:
        set_calls.append((watermark_kind, counter))

    async def fake_build(*, scopes: Scopes) -> None:
        builds.append(scopes)

    monkeypatch.setattr(engine, "transaction", fake_transaction)
    monkeypatch.setattr(tasks_mod.Watermark, "read", fake_read)
    monkeypatch.setattr(tasks_mod.Watermark, "set_value", fake_set_value)
    monkeypatch.setattr(tasks_mod, build_target, fake_build)
    monkeypatch.setattr(settings, threshold_field, threshold)

    key = frozenset({user})
    asyncio.run(task_cls().run(key))

    should_build = current - last >= threshold
    assert (builds == [key]) is should_build
    assert set_calls == ([(kind, current)] if should_build else [])


def report_with(significant_best: str | None, per_config: dict[str, float]) -> EvalReport:
    return EvalReport(
        n=2,
        hit_at_k=0.5,
        ndcg_at_k=0.4,
        mrr=0.5,
        mean_judge=None,
        per_config=per_config,
        comparison=None,
        significant_best=significant_best,
    )


@pytest.mark.parametrize(
    ("significant_best", "per_config", "expected_best"),
    [
        (
            None,
            {"multihop_max_hops=2": 0.5, "multihop_max_hops=0": 0.4},
            "multihop_max_hops=2",
        ),
        (
            "multihop_max_hops=0",
            {"multihop_max_hops=2": 0.5, "multihop_max_hops=0": 0.4},
            "multihop_max_hops=2",
        ),
        (None, {}, None),
    ],
    ids=["no-win", "significant-win", "empty-per-config"],
)
def test_self_improve_stores_the_scorecard_without_mutating_live_settings(
    monkeypatch: pytest.MonkeyPatch,
    significant_best: str | None,
    per_config: dict[str, float],
    expected_best: str | None,
) -> None:
    writes: list[tuple[Watermark.Kind, dict[str, object] | None]] = []

    async def stub_run_eval(questions: object, k: int = 8, user: User | None = None) -> EvalReport:
        return report_with(significant_best, per_config)

    @asynccontextmanager
    async def fake_transaction(user: User) -> AsyncGenerator[None]:
        yield None

    async def fake_set_value(
        session: object,
        scopes: Scopes,
        kind: Watermark.Kind,
        counter: int = 0,
        payload: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        writes.append((kind, payload))

    monkeypatch.setattr(tasks_mod, "run_eval", stub_run_eval)
    monkeypatch.setattr(engine, "transaction", fake_transaction)
    monkeypatch.setattr(tasks_mod.Watermark, "set_value", fake_set_value)
    monkeypatch.setattr(settings, "multihop_max_hops", 0)

    asyncio.run(SelfImproveTask().run(frozenset({settings.system_user_id})))

    (kind, payload) = writes[0]
    assert [kind for kind, _ in writes] == [Watermark.Kind.scorecard]
    assert payload is not None
    assert payload["best"] == expected_best
    assert payload["significant_best"] == significant_best
    assert settings.multihop_max_hops == 0


def test_backup_task_fire_cron_runs_the_scheduled_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = []

    async def fake_scheduled_backup() -> object:
        ran.append(True)
        return SimpleNamespace(bytes=7, path="/backups/x.dump")

    async def fail_fan_out(task: ScheduledTask) -> None:
        raise AssertionError("a backup must never fan out across users")

    monkeypatch.setattr(tasks_mod, "scheduled_backup", fake_scheduled_backup)
    asyncio.run(BackupTask().fire_cron(fail_fan_out, schedule=cast("Schedule", None)))
    assert ran == [True]


def test_backup_task_run_is_never_fanned_out_per_user() -> None:
    with pytest.raises(NotImplementedError, match="system pass"):
        asyncio.run(BackupTask().run(frozenset({uuid.uuid4()})))


@pytest.mark.parametrize("enabled", [True, False])
def test_backup_task_registers_only_the_cron_and_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    monkeypatch.setattr(settings, "backup_enabled", enabled)
    monkeypatch.setattr(settings, "backup_cron", "0 2 * * *")
    pg = RecordingPg()

    async def unused_fan_out(task: ScheduledTask) -> None:
        pass

    BackupTask().register(cast(PgQueuer, pg), fan_out=unused_fan_out)

    assert pg.entrypoints == {}  # never a per-user entrypoint, whatever the flag
    assert len(pg.schedules) == (1 if enabled else 0)
    if enabled:
        assert pg.schedules[0][:2] == ("aizk_cron_backup", "0 2 * * *")
