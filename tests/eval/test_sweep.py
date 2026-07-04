import math
import sys
import types
import uuid

import dbutil
import numpy as np
import pytest
from eval_util import FakeMeter, install_constant_recall, install_fake_meter
from hypothesis import given
from hypothesis import strategies as st

import aizk.eval.sweep as sweep
from aizk.config import settings
from aizk.eval import QA
from aizk.eval.sweep import (
    AXIS_FIELDS,
    HALFVEC_BYTES,
    ConfigResult,
    Measurement,
    SweepConfig,
    SweepMatrix,
    SweepReport,
    build_matrix,
    config_result,
    open_meter,
    percentile,
    run_sweep,
)


def sweep_matrices() -> st.SearchStrategy[SweepMatrix]:
    """A matrix whose axes hold a few values or none, the grid `build_matrix` expands and pins.

    Each axis draws a small unique list, some empty, so one property covers both the cartesian
    product and the empty-axis-holds-the-live-setting fallback, bounded so the grid stays small.
    """
    return st.builds(
        SweepMatrix,
        embed_model=st.lists(st.sampled_from(["m1", "m2"]), max_size=2, unique=True),
        embed_dim=st.lists(st.sampled_from([512, 1024, 2048]), max_size=2, unique=True),
        rerank=st.lists(st.booleans(), max_size=2, unique=True),
        ppr=st.lists(st.booleans(), max_size=2, unique=True),
        query_routing=st.lists(st.booleans(), max_size=2, unique=True),
    )


@given(matrix=sweep_matrices())
def test_build_matrix_is_the_labeled_cartesian_product_of_the_axes(matrix: SweepMatrix) -> None:
    """The grid is every axis combination, each labeled by and pinned to its assignment."""
    axes = matrix.axes()
    configs = build_matrix(matrix)

    assert len(configs) == math.prod(len(values) for values in axes.values())
    for config in configs:
        assert set(config.overrides) == set(AXIS_FIELDS)
        assert config.label == ",".join(f"{field}={config.overrides[field]}" for field in axes)
        assert all(config.overrides[field] in axes[field] for field in AXIS_FIELDS)
    # an axis the matrix leaves empty is held at the live setting across the whole grid
    for field in AXIS_FIELDS:
        if not getattr(matrix, field):
            assert {config.overrides[field] for config in configs} == {getattr(settings, field)}


@given(
    values=st.lists(
        st.floats(min_value=0.0, max_value=500.0, allow_nan=False, allow_infinity=False),
        max_size=12,
    ),
    q=st.sampled_from([50.0, 95.0, 99.0]),
)
def test_percentile_is_numpy_on_a_sample_and_zero_when_empty(
    values: list[float], q: float
) -> None:
    """The percentile matches numpy on a filled sample and short-circuits an empty one to zero."""

    if values:
        assert percentile(values, q) == float(np.percentile(values, q))
    else:
        assert percentile(values, q) == 0.0


@given(
    embed_dim=st.integers(min_value=1, max_value=4096),
    metric_values=st.lists(
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
        min_size=3,
        max_size=3,
    ),
    latencies=st.lists(
        st.floats(min_value=0.0, max_value=500.0, allow_nan=False, allow_infinity=False),
        max_size=10,
    ),
    override=st.booleans(),
)
def test_config_result_reads_metrics_latency_and_halfvec_footprint(
    embed_dim: int, metric_values: list[float], latencies: list[float], override: bool
) -> None:
    """A report row reads the ranx metrics, the latency percentiles, and the halfvec footprint."""
    overrides = {"embed_dim": embed_dim} if override else {}
    config = SweepConfig(overrides=overrides, label="probe")
    metrics = ["recall@4", "ndcg@4", "mrr"]
    scored = dict(zip(metrics, metric_values, strict=True))
    measurement = Measurement(scores={}, latencies=latencies, peak_host_gb=2.0, peak_gpu_gb=1.0)

    row = config_result(config, scored, measurement, metrics)

    assert isinstance(row, ConfigResult)
    assert (row.recall_at_k, row.ndcg_at_k, row.mrr) == tuple(metric_values)
    assert row.latency_p50_ms == percentile(latencies, 50)
    assert row.latency_p95_ms == percentile(latencies, 95)
    assert (row.peak_host_gb, row.peak_gpu_gb) == (2.0, 1.0)
    width = embed_dim if override else settings.embed_dim
    assert row.storage_bytes_per_vector == width * HALFVEC_BYTES


def test_open_meter_returns_the_mainboard_meter(monkeypatch: pytest.MonkeyPatch) -> None:
    """`open_meter` lazily imports mainboard and hands back the meter it builds."""
    fake_module = types.ModuleType("mainboard")
    monkeypatch.setattr(fake_module, "meter", FakeMeter, raising=False)
    monkeypatch.setitem(sys.modules, "mainboard", fake_module)

    assert isinstance(open_meter(), FakeMeter)


@pytest.mark.parametrize("mode", ["compare", "single", "synth", "empty"], ids=lambda mode: mode)
def test_run_sweep_scores_each_config_or_short_circuits(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """A grid scores quality, latency, and memory per config, comparing only on a multi-config run.

    compare: two configs score and a comparison table is built. single: one config scores with no
    table. synth: a null gold is replaced by questions synthesized from the corpus. empty: an empty
    gold short-circuits to an empty scorecard before any config is scored.
    """
    if mode != "empty":
        install_constant_recall(monkeypatch, sweep, "alpha holds")
        install_fake_meter(monkeypatch)
    toggles = [False, True] if mode == "compare" else [True]
    matrix = SweepMatrix(rerank=toggles, ppr=[True], query_routing=[False])

    if mode == "synth":

        async def stub_build_questions(
            questions: list[str] | None, principal_id: uuid.UUID
        ) -> list[QA]:
            return [QA(question="what does alpha hold", expected="alpha holds")]

        monkeypatch.setattr(sweep, "build_questions", stub_build_questions)
        report = dbutil.run(run_sweep(questions=None, k=4, matrix=matrix))
    elif mode == "empty":
        report = dbutil.run(run_sweep(k=4, gold=[]))
    else:
        gold = [QA(question="what does alpha hold", expected="alpha holds")]
        report = dbutil.run(run_sweep(k=4, matrix=matrix, gold=gold))

    assert isinstance(report, SweepReport)
    if mode == "empty":
        assert report.n == 0 and report.results == []
        assert report.comparison is None and report.best_label is None
        return

    assert report.n == 1
    assert len(report.results) == len(toggles)
    assert (report.comparison is not None) == (mode == "compare")
    assert report.best_label is None  # the stub ignores the toggles, so a tie never flips the base
    for row in report.results:
        assert row.recall_at_k == 1.0  # the stub always surfaces the gold fact
        assert row.latency_p50_ms >= 0.0 and row.latency_p95_ms >= 0.0
        assert (row.peak_host_gb, row.peak_gpu_gb) == (1.5, 0.5)
        assert row.storage_bytes_per_vector == settings.embed_dim * HALFVEC_BYTES


def one_row(label: str = "rerank=False,ppr=True", storage: int = 2048) -> ConfigResult:
    """A single filled report row, the render fixture the table pins one line of output over.

    label: the axis assignment the row reports.
    storage: the halfvec footprint the row carries, read back verbatim in the rendered table.
    """
    return ConfigResult(
        label=label,
        recall_at_k=1.0,
        ndcg_at_k=1.0,
        mrr=1.0,
        latency_p50_ms=1.2,
        latency_p95_ms=3.4,
        peak_host_gb=1.5,
        peak_gpu_gb=0.5,
        storage_bytes_per_vector=storage,
    )


@pytest.mark.parametrize(
    ("report", "needles"),
    [
        (
            SweepReport(
                n=1,
                k=4,
                results=[one_row()],
                comparison="table",
                best_label="rerank=False,ppr=True",
            ),
            ["n=1 k=4", "best=rerank=False,ppr=True", "recall@4=", "p50=", "p95=", "store=2048b"],
        ),
        (
            SweepReport(n=1, k=4, results=[one_row()], comparison=None, best_label=None),
            ["best=none"],
        ),
        (SweepReport(n=0, k=4, results=[], comparison=None, best_label=None), ["no configs"]),
    ],
    ids=["filled", "no-best", "empty"],
)
def test_render_renders_a_row_per_config(report: SweepReport, needles: list[str]) -> None:
    """The rendered table carries the header, the metrics, latency, memory, and storage per row."""
    rendered = report.render()

    assert all(needle in rendered for needle in needles)
