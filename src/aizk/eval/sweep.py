import time
from itertools import product

import jinja2
import numpy as np
from loguru import logger
from mainboard import Meter, meter
from patos import FrozenModel
from pydantic import Field
from ranx import Qrels, Run

from ..config import settings
from ..retrieval import recall
from ..store.identity import User
from .harness import build_questions, retrieved_scores, score_comparison, swept_settings
from .models import QA

# The retrieval toggles that can be compared against one fixed stored index.
AXIS_FIELDS = ("multihop_max_hops",)

type AxisValue = str | int | bool


class SweepMatrix(FrozenModel):
    """The values each config axis ranges over, the cartesian grid the sweep walks."""

    multihop_max_hops: list[int] = Field(
        default_factory=lambda: sorted({0, settings.multihop_max_hops})
    )

    def axes(self) -> dict[str, list[AxisValue]]:
        """Resolve each axis to its swept values, falling back to the live setting when
        empty."""
        return {
            field: list(getattr(self, field)) or [getattr(settings, field)]
            for field in AXIS_FIELDS
        }


class SweepConfig(FrozenModel):
    """One point in the grid, the settings overlay and the label its scores are keyed under."""

    overrides: dict[str, AxisValue]
    label: str


class ConfigResult(FrozenModel):
    """The measured quality, speed, and footprint of one swept config, a row of the report
    table."""

    label: str
    recall_at_k: float
    ndcg_at_k: float
    mrr: float
    latency_p50_ms: float
    latency_p95_ms: float
    peak_host_gb: float
    peak_gpu_gb: float


# renders a sweep scorecard as a compact text table, one row per config, the numbers already
# rounded so the template stays structural.
_TEMPLATE = jinja2.Template(
    """\
{%- if not results %}
sweep scored no configs, no gold to evaluate
{%- else -%}
n={{ n }} k={{ k }} best={{ best_label or "none" }}
{% for row in results %}  {{
    "{}: recall@{}={} ndcg@{}={} mrr={} p50={}ms p95={}ms host={}gb gpu={}gb".format(
        row.label, k, row.recall_at_k, k, row.ndcg_at_k, row.mrr, row.latency_p50_ms,
        row.latency_p95_ms, row.peak_host_gb, row.peak_gpu_gb,
    )
}}
{% endfor -%}
{%- endif %}""",
    trim_blocks=True,
    lstrip_blocks=True,
)


class SweepReport(FrozenModel):
    """The full sweep scorecard, every config's quality, latency, and footprint side by side."""

    n: int
    k: int
    results: list[ConfigResult]
    comparison: str | None
    best_label: str | None

    def render(self) -> str:
        """Render this scorecard as a compact text table, one row per config."""
        results = [
            {
                "label": row.label,
                "recall_at_k": round(row.recall_at_k, 3),
                "ndcg_at_k": round(row.ndcg_at_k, 3),
                "mrr": round(row.mrr, 3),
                "latency_p50_ms": round(row.latency_p50_ms, 1),
                "latency_p95_ms": round(row.latency_p95_ms, 1),
                "peak_host_gb": round(row.peak_host_gb, 2),
                "peak_gpu_gb": round(row.peak_gpu_gb, 2),
            }
            for row in self.results
        ]
        return _TEMPLATE.render(
            n=self.n, k=self.k, best_label=self.best_label, results=results
        ).strip()


class Measurement(FrozenModel):
    """The raw measurement of one config before it is scored into a report row."""

    scores: dict[str, dict[str, float]]
    latencies: list[float]
    peak_host_gb: float
    peak_gpu_gb: float


def build_matrix(matrix: SweepMatrix) -> list[SweepConfig]:
    """Expand the axis grid into the list of configs the sweep runs, in cartesian order."""
    axes = matrix.axes()
    configs: list[SweepConfig] = []
    for combo in product(*axes.values()):
        overrides = dict(zip(axes, combo, strict=True))
        label = ",".join(f"{field}={overrides[field]}" for field in axes)
        configs.append(SweepConfig(overrides=overrides, label=label))
    return configs


def open_meter() -> Meter:
    """Open a mainboard runtime-metrics meter, isolated so tests can replace the host probe."""
    return meter()


def percentile(values: list[float], q: float) -> float:
    """Return the qth percentile of the values, zero for an empty sample."""
    return float(np.percentile(values, q)) if values else 0.0


async def measure_config(
    config: SweepConfig,
    gold: list[QA],
    user: User,
    k: int,
) -> Measurement:
    """Recall every gold question under one config, timing each and metering the memory peak."""
    scores: dict[str, dict[str, float]] = {}
    latencies: list[float] = []
    with swept_settings(**config.overrides), open_meter() as meter:
        for index, qa in enumerate(gold):
            start = time.perf_counter()
            result = await recall(qa.question, user=user, k=k)
            latencies.append((time.perf_counter() - start) * 1000.0)
            scores[f"q{index}"] = retrieved_scores(qa, result)
            meter.sample()
    return Measurement(
        scores=scores,
        latencies=latencies,
        peak_host_gb=meter.peak_host_gb,
        peak_gpu_gb=meter.peak_gpu_gb,
    )


async def measure_configs(
    configs: list[SweepConfig], gold: list[QA], user: User, k: int
) -> dict[str, Measurement]:
    """Measure every config in the grid and return its raw scores, latencies, and memory
    peaks."""
    return {config.label: await measure_config(config, gold, user, k) for config in configs}


def config_runs(measured: dict[str, Measurement]) -> list[Run]:
    """Render each config's measured scores as a named ranx Run, in measured order."""
    return [Run(measurement.scores, name=label) for label, measurement in measured.items()]


def score_runs(
    qrels: Qrels, runs: list[Run], baseline: str, metrics: list[str]
) -> tuple[dict[str, dict[str, float]], str | None, str | None]:
    """Score the configs' runs and compare them when there are two runs and two queries."""
    return score_comparison(qrels, runs, metrics, baseline)


def config_result(
    config: SweepConfig,
    scored: dict[str, float],
    measurement: Measurement,
    metrics: list[str],
) -> ConfigResult:
    """Assemble one report row from a config's ranx scores, latencies, and memory peaks."""
    return ConfigResult(
        label=config.label,
        recall_at_k=scored[metrics[0]],
        ndcg_at_k=scored[metrics[1]],
        mrr=scored[metrics[2]],
        latency_p50_ms=percentile(measurement.latencies, 50),
        latency_p95_ms=percentile(measurement.latencies, 95),
        peak_host_gb=measurement.peak_host_gb,
        peak_gpu_gb=measurement.peak_gpu_gb,
    )


async def run_sweep(
    questions: list[str] | None = None,
    k: int = 8,
    user: User | None = None,
    matrix: SweepMatrix | None = None,
    gold: list[QA] | None = None,
) -> SweepReport:
    """Sweep the config grid, scoring quality, latency, and memory for each config side by
    side."""
    user = user or User.system()
    matrix = matrix or SweepMatrix()
    if gold is None:
        items = await build_questions(questions, user)
        gold = [qa for qa in items if qa.expected is not None]
    configs = build_matrix(matrix)
    if not gold:
        return SweepReport(n=0, k=k, results=[], comparison=None, best_label=None)
    metrics = [f"recall@{k}", f"ndcg@{k}", "mrr"]
    qrels = Qrels({f"q{index}": {"rel": 1} for index in range(len(gold))})
    measured = await measure_configs(configs, gold, user, k)
    scored, comparison, best_label = score_runs(
        qrels, config_runs(measured), configs[0].label, metrics
    )
    results = [
        config_result(config, scored[config.label], measured[config.label], metrics)
        for config in configs
    ]
    logger.info(
        "sweep scored {configs} configs over {n} items, best {best}",
        configs=len(configs),
        n=len(gold),
        best=best_label,
    )
    return SweepReport(
        n=len(gold), k=k, results=results, comparison=comparison, best_label=best_label
    )
