import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from hypothesis import given
from hypothesis import strategies as st

import aizk.background.tasks as tasks_mod
from aizk.background.tasks import (
    CommunitiesTask,
    CurationReviewTask,
    DecayTask,
    DedupTask,
    InsightTask,
    ProfileRefreshTask,
    RaptorTask,
    ScheduledTask,
    SelfImproveTask,
    SessionPromoteTask,
    config_from_label,
)
from aizk.config import settings
from aizk.eval import EvalReport
from aizk.store import Watermark

# the closed axis vocabulary the flip label is built from, the toggles recall reads back.
axes = st.sampled_from(["rerank", "ppr", "communities", "raptor"])


@given(config=st.dictionaries(axes, st.booleans(), min_size=1, max_size=4))
def test_config_from_label_round_trips_every_toggle(config: dict[str, bool]) -> None:
    """The flip label parses back to the exact axis-to-Bool override the winning sweep named."""
    label = ",".join(f"{axis}={value}" for axis, value in config.items())
    assert config_from_label(label) == config


def test_the_roster_carries_every_maintenance_pass() -> None:
    """`implementations()` is the whole roster of passes, each name its own settings prefix."""
    names = {task_cls.name for task_cls in ScheduledTask.implementations()}
    assert names == {
        "decay",
        "dedup",
        "communities",
        "raptor",
        "profile_refresh",
        "self_improve",
        "session_promote",
        "insight",
        "curation_review",
    }


def test_entrypoint_names_are_prefixed_and_injective() -> None:
    """Each task mints a distinct queue and cron entrypoint, both name-prefixed, none colliding."""
    tasks = [task_cls() for task_cls in ScheduledTask.implementations()]
    queue_names = {task.queue_entrypoint for task in tasks}
    cron_names = {task.cron_entrypoint for task in tasks}
    assert all(task.queue_entrypoint == f"aizk_task_{task.name}" for task in tasks)
    assert all(task.cron_entrypoint == f"aizk_cron_{task.name}" for task in tasks)
    assert len(queue_names) == len(cron_names) == len(tasks)
    assert queue_names.isdisjoint(cron_names)


@given(
    flags=st.lists(st.booleans(), min_size=9, max_size=9),
    cron=st.sampled_from(["0 3 * * *", "30 4 * * 0"]),
)
def test_each_task_reads_its_own_cadence_and_flag_off_settings(
    flags: list[bool], cron: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every pass's `expression` and `enabled` come straight off its own name-keyed settings."""
    tasks = [task_cls() for task_cls in ScheduledTask.implementations()]
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
        (CurationReviewTask, "review_curated_groups"),
    ],
)
def test_thin_passes_delegate_under_the_principal(
    monkeypatch: pytest.MonkeyPatch, task_cls: type[ScheduledTask], target: str
) -> None:
    """Each thin maintenance pass forwards its principal to the graph routine it wraps."""
    principal = uuid.uuid4()
    seen: list[uuid.UUID] = []

    async def record(*, principal_id: uuid.UUID, **kwargs: float) -> None:
        seen.append(principal_id)

    monkeypatch.setattr(tasks_mod, target, record)
    asyncio.run(task_cls().run(principal))
    assert seen == [principal]


class GateSession:
    """A fake scoped session whose only read is the latest-fact count the growth gate measures.

    current: the latest-fact count the gate compares against its high-water mark.
    """

    def __init__(self, current: int) -> None:
        self.current = current

    async def scalar(self, statement: object) -> int:
        """Return the seeded latest-fact count for the gate's count query.

        statement: the count select, ignored since the count is fixed per test.
        """
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
    """A growth-gated pass rebuilds and advances its high-water only when the graph grew enough.

    The gate's own invariant: build iff the latest-fact count gained at least the threshold since
    the last build, and on a build the high-water mark advances to the current count so the next
    pass measures growth from here; below the threshold nothing is built and the mark holds.
    """
    principal = uuid.uuid4()
    builds: list[uuid.UUID] = []
    set_calls: list[tuple[Watermark.Kind, int]] = []

    @asynccontextmanager
    async def fake_acting_as(principal_id: uuid.UUID) -> AsyncIterator[GateSession]:
        yield GateSession(current)

    async def fake_read(
        session: object, owner_id: uuid.UUID, watermark_kind: Watermark.Kind, **kw: object
    ) -> int:
        return last

    async def fake_set_value(
        session: object,
        owner_id: uuid.UUID,
        watermark_kind: Watermark.Kind,
        counter: int = 0,
        **kw: object,
    ) -> None:
        set_calls.append((watermark_kind, counter))

    async def fake_build(*, principal_id: uuid.UUID) -> None:
        builds.append(principal_id)

    monkeypatch.setattr(tasks_mod, "acting_as", fake_acting_as)
    monkeypatch.setattr(tasks_mod.Watermark, "read", fake_read)
    monkeypatch.setattr(tasks_mod.Watermark, "set_value", fake_set_value)
    monkeypatch.setattr(tasks_mod, build_target, fake_build)
    monkeypatch.setattr(settings, threshold_field, threshold)

    asyncio.run(task_cls().run(principal))

    should_build = current - last >= threshold
    assert (builds == [principal]) is should_build
    assert set_calls == ([(kind, current)] if should_build else [])


def report_with(significant_best: str | None, per_config: dict[str, float]) -> EvalReport:
    """An EvalReport carrying a chosen flip signal, the self-improve pass's input.

    significant_best: the toggle label a sweep significantly won under, or null for no win.
    per_config: hit-at-k per toggle, empty to drive the no-best branch.
    """
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
    ("significant_best", "per_config", "expected_best", "flipped"),
    [
        (
            None,
            {"rerank=True,ppr=True": 0.5, "rerank=False,ppr=True": 0.4},
            "rerank=True,ppr=True",
            False,
        ),
        (
            "rerank=False,ppr=True",
            {"rerank=True,ppr=True": 0.5, "rerank=False,ppr=True": 0.4},
            "rerank=True,ppr=True",
            True,
        ),
        (None, {}, None, False),
    ],
    ids=["no-win", "significant-win", "empty-per-config"],
)
def test_self_improve_stores_the_scorecard_and_flips_only_on_a_significant_win(
    monkeypatch: pytest.MonkeyPatch,
    significant_best: str | None,
    per_config: dict[str, float],
    expected_best: str | None,
    flipped: bool,
) -> None:
    """The scorecard is always stored, and the live settings move only on a significance flip.

    An admin can always read what recall scored, but the global `settings` singleton flips in
    process only when the sweep names a significant winner, never on a raw delta or noise, and the
    stored best is the argmax over per_config or null when no config was scored.
    """
    writes: list[tuple[Watermark.Kind, dict[str, object] | None]] = []

    async def stub_run_eval(
        questions: object, k: int = 8, principal_id: uuid.UUID | None = None
    ) -> EvalReport:
        return report_with(significant_best, per_config)

    @asynccontextmanager
    async def fake_acting_as(principal_id: uuid.UUID) -> AsyncIterator[None]:
        yield None

    async def fake_set_value(
        session: object,
        owner_id: uuid.UUID,
        kind: Watermark.Kind,
        counter: int = 0,
        payload: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        writes.append((kind, payload))

    monkeypatch.setattr(tasks_mod, "run_eval", stub_run_eval)
    monkeypatch.setattr(tasks_mod, "acting_as", fake_acting_as)
    monkeypatch.setattr(tasks_mod.Watermark, "set_value", fake_set_value)
    monkeypatch.setattr(settings, "rerank", True)
    monkeypatch.setattr(settings, "ppr", False)

    asyncio.run(SelfImproveTask().run(settings.system_principal_id))

    (kind, payload) = writes[0]
    assert [kind for kind, _ in writes] == [Watermark.Kind.scorecard]
    assert payload is not None
    assert payload["best"] == expected_best
    assert payload["significant_best"] == significant_best
    if flipped:
        assert settings.rerank is False and settings.ppr is True
    else:
        assert settings.rerank is True and settings.ppr is False
