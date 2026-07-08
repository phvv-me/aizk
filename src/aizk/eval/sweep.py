import time
import uuid
from itertools import product
from typing import TYPE_CHECKING

import jinja2
import numpy as np
from loguru import logger
from patos import FrozenModel
from pydantic import Field

from ..config import settings
from ..retrieval import recall
from .harness import build_questions, retrieved_scores, significant_winner, swept_settings
from .qa import QA

if TYPE_CHECKING:
    from mainboard import Meter
    from ranx import Qrels, Run

# a pgvector halfvec stores each element in two bytes, so a stored vector's footprint is the
# embedding width times this, the storage signal the Matryoshka dimensions axis trades quality for.
HALFVEC_BYTES = 2

# the axes the sweep ranges a config over, named exactly as their Settings fields so an axis value
# overlays straight onto a Settings copy, the embed width and the three retrieval toggles.
AXIS_FIELDS = (
    "embed_model",
    "embed_dim",
    "rerank",
    "ppr",
    "query_routing",
)

type AxisValue = str | int | bool


class SweepMatrix(FrozenModel):
    """The values each config axis ranges over, the cartesian grid the sweep walks.

    Every axis is a list, and an empty one is filled with the live settings' value so the grid
    holds that axis fixed at the current config. The retrieval toggles default to both states since
    they cost nothing to flip against the live corpus, while the embedder axes default empty since
    a different embed model or width needs a matching re-embedded corpus to score against.

    embed_model: the served embed model ids to range over, empty to hold the live model.
    embed_dim: the Matryoshka widths to range over, such as 512, 1024, and 2048, empty to hold one.
    rerank: whether the cross-encoder rerank lane is on, both states by default.
    ppr: whether the multi-hop personalized-pagerank lane is on, both states by default.
    query_routing: whether recall narrows the mix to the query's route, both states by default.
    """

    embed_model: list[str] = Field(default_factory=list)
    embed_dim: list[int] = Field(default_factory=list)
    rerank: list[bool] = Field(default_factory=lambda: [False, True])
    ppr: list[bool] = Field(default_factory=lambda: [False, True])
    query_routing: list[bool] = Field(default_factory=lambda: [False, True])

    def axes(self) -> dict[str, list[AxisValue]]:
        """Resolve each axis to its swept values, falling back to the live setting when empty."""
        return {
            field: list(getattr(self, field)) or [getattr(settings, field)]
            for field in AXIS_FIELDS
        }


class SweepConfig(FrozenModel):
    """One point in the grid, the settings overlay and the label its scores are keyed under.

    overrides: the per-axis settings overlay applied to the live config for this run.
    label: the comma-joined axis assignment, the key the ranx comparison reads each run under.
    """

    overrides: dict[str, AxisValue]
    label: str


class ConfigResult(FrozenModel):
    """The measured quality, speed, and footprint of one swept config, a row of the report table.

    label: the axis assignment this row reports.
    recall_at_k: ranx recall@k of the config over the shared qrels.
    ndcg_at_k: ranx ndcg@k of the config, rewarding the expected fact ranking high.
    mrr: ranx mean reciprocal rank of the expected fact under the config.
    latency_p50_ms: median per-query recall wall time in milliseconds.
    latency_p95_ms: tail per-query recall wall time in milliseconds.
    peak_host_gb: highest host memory in use across the config's recalls, in gibibytes.
    peak_gpu_gb: highest total GPU memory in use across the config's recalls, in gibibytes.
    storage_bytes_per_vector: halfvec footprint of one stored vector at the config's embed width.
    """

    label: str
    recall_at_k: float
    ndcg_at_k: float
    mrr: float
    latency_p50_ms: float
    latency_p95_ms: float
    peak_host_gb: float
    peak_gpu_gb: float
    storage_bytes_per_vector: int


# renders a sweep scorecard as a compact text table, one row per config, the numbers already
# rounded so the template stays structural.
_TEMPLATE = jinja2.Template(
    """\
{%- if not results %}
sweep scored no configs, no gold to evaluate
{%- else -%}
n={{ n }} k={{ k }} best={{ best_label or "none" }}
{% for row in results %}  {{
    "{}: recall@{}={} ndcg@{}={} mrr={} p50={}ms p95={}ms host={}gb gpu={}gb store={}b".format(
        row.label, k, row.recall_at_k, k, row.ndcg_at_k, row.mrr, row.latency_p50_ms,
        row.latency_p95_ms, row.peak_host_gb, row.peak_gpu_gb, row.storage_bytes_per_vector,
    )
}}
{% endfor -%}
{%- endif %}""",
    trim_blocks=True,
    lstrip_blocks=True,
)


class SweepReport(FrozenModel):
    """The full sweep scorecard, every config's quality, latency, and footprint side by side.

    n: number of gold items each config was scored over.
    k: number of hits and seed facts each recall surfaced.
    results: one measured row per swept config, in grid order.
    comparison: the ranx.compare significance table across the configs, null for a single config.
    best_label: the config that significantly beats the first on ndcg, the sweep's pick, null when
        no config clears the significance threshold over the baseline.
    """

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
                "storage_bytes_per_vector": row.storage_bytes_per_vector,
            }
            for row in self.results
        ]
        return _TEMPLATE.render(
            n=self.n, k=self.k, best_label=self.best_label, results=results
        ).strip()


class Measurement(FrozenModel):
    """The raw measurement of one config before it is scored into a report row.

    scores: the per-query retrieved-doc scores, the ranx run for this config.
    latencies: per-query recall wall times in milliseconds.
    peak_host_gb: highest host memory in use across the config's recalls, in gibibytes.
    peak_gpu_gb: highest total GPU memory in use across the config's recalls, in gibibytes.
    """

    scores: dict[str, dict[str, float]]
    latencies: list[float]
    peak_host_gb: float
    peak_gpu_gb: float


def build_matrix(matrix: SweepMatrix) -> list[SweepConfig]:
    """Expand the axis grid into the list of configs the sweep runs, in cartesian order.

    Resolves each axis against the live settings so an empty axis holds its current value, then
    takes the cartesian product across the axes, labeling each config by its axis assignment so the
    ranx comparison and the report table read the same key.

    matrix: the per-axis values to range over.
    """
    axes = matrix.axes()
    configs: list[SweepConfig] = []
    for combo in product(*axes.values()):
        overrides = dict(zip(axes, combo, strict=True))
        label = ",".join(f"{field}={overrides[field]}" for field in axes)
        configs.append(SweepConfig(overrides=overrides, label=label))
    return configs


def open_meter() -> Meter:
    """Open a mainboard runtime-metrics meter, the time and memory probe wrapped around a config.

    Imported lazily like the ranx aggregation so the dev-only profiling dependency stays off the
    default install, and isolated here so a test swaps in a deterministic meter without a host.
    """
    from mainboard import meter

    return meter()


def percentile(values: list[float], q: float) -> float:
    """Return the qth percentile of the values, zero for an empty sample.

    values: the measured samples, such as per-query latencies.
    q: the percentile to read, 50 for the median and 95 for the tail.
    """
    return float(np.percentile(values, q)) if values else 0.0


async def measure_config(
    config: SweepConfig,
    gold: list[QA],
    user_id: uuid.UUID,
    k: int,
) -> Measurement:
    """Recall every gold question under one config, timing each and metering the memory peak.

    Overlays the config's axes onto the live settings, then recalls each gold question inside a
    mainboard meter, recording per-query wall time and scoring the ranking, so the sweep reads
    quality, latency, and memory off the one pass.

    config: the grid point whose axes overlay the live settings.
    gold: the evaluation items that carry an expected fact.
    user_id: identity whose row level security visibility scopes the recall.
    k: number of hits and seed facts each recall surfaces.
    """
    scores: dict[str, dict[str, float]] = {}
    latencies: list[float] = []
    with swept_settings(**config.overrides), open_meter() as meter:
        for index, qa in enumerate(gold):
            start = time.perf_counter()
            result = await recall(qa.question, user_id=user_id, k=k)
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
    configs: list[SweepConfig], gold: list[QA], user_id: uuid.UUID, k: int
) -> dict[str, Measurement]:
    """Measure every config in the grid and return its raw scores, latencies, and memory peaks.

    configs: the grid points to measure, in the order the report lists them.
    gold: the evaluation items that carry an expected fact.
    user_id: identity whose row level security visibility scopes the recall.
    k: number of hits and seed facts each recall surfaces.
    """
    return {config.label: await measure_config(config, gold, user_id, k) for config in configs}


def config_runs(measured: dict[str, Measurement]) -> list[Run]:
    """Render each config's measured scores as a named ranx Run, in measured order.

    measured: the raw measurement per config label, measure_configs' own output.
    """
    from ranx import Run

    return [Run(measurement.scores, name=label) for label, measurement in measured.items()]


def score_runs(
    qrels: Qrels, runs: list[Run], baseline: str, metrics: list[str]
) -> tuple[dict[str, dict[str, float]], str | None, str | None]:
    """Score the configs' runs, a full significance comparison when there are at least two.

    qrels: the shared relevance judgments every run is scored against.
    runs: the configs' named ranx runs, in grid order.
    baseline: the label the significance comparison and single-run fallback both key off of.
    metrics: the ranx metric list, recall@k first and ndcg@k second.

    Returns each run's per-metric scores keyed by label, the rendered comparison table (null for a
    single config), and the label that significantly beats the baseline (null when none does).
    """
    from ranx import compare, evaluate

    if len(runs) < 2:
        single = evaluate(qrels, runs[0], metrics)
        assert isinstance(single, dict)  # a metric list always evaluates to a per-metric dict
        return {baseline: single}, None, None
    report = compare(qrels, runs, metrics=metrics)
    scored = {run.name: report.results[run.name] for run in runs}
    best = significant_winner(report.to_dict(), baseline, metrics[1], settings.self_improve_max_p)
    return scored, str(report), best


def config_result(
    config: SweepConfig,
    scored: dict[str, float],
    measurement: Measurement,
    metrics: list[str],
) -> ConfigResult:
    """Assemble one report row from a config's ranx scores, latencies, and memory peaks.

    config: the grid point this row reports, read for its embed width footprint.
    scored: the config's ranx metric values, recall first, ndcg second, mrr third.
    measurement: the config's raw latencies and memory peaks.
    metrics: the ranx metric list, recall@k first and ndcg@k second as run_sweep orders them.
    """
    embed_dim = config.overrides.get("embed_dim", settings.embed_dim)
    return ConfigResult(
        label=config.label,
        recall_at_k=float(scored[metrics[0]]),
        ndcg_at_k=float(scored[metrics[1]]),
        mrr=float(scored[metrics[2]]),
        latency_p50_ms=percentile(measurement.latencies, 50),
        latency_p95_ms=percentile(measurement.latencies, 95),
        peak_host_gb=measurement.peak_host_gb,
        peak_gpu_gb=measurement.peak_gpu_gb,
        storage_bytes_per_vector=int(embed_dim) * HALFVEC_BYTES,
    )


async def run_sweep(
    questions: list[str] | None = None,
    k: int = 8,
    user_id: uuid.UUID | None = None,
    matrix: SweepMatrix | None = None,
    gold: list[QA] | None = None,
) -> SweepReport:
    """Sweep the config grid, scoring quality, latency, and memory for each config side by side.

    For each config, recalls every gold question on the tuned settings inside a meter and scores
    the ranking into a ranx run, so `ranx.compare` reads recall, ndcg, mrr, and significance across
    configs while the per-config latency percentiles and memory peaks ride alongside.

    questions: the caller's questions, or null to synthesize gold from sampled facts.
    k: number of hits and seed facts each recall surfaces.
    user_id: identity whose row level security visibility scopes the recall and the sample,
        the system user when null.
    matrix: the axis grid to sweep, the default toggle grid when null.
    gold: pre-built evaluation items, such as a benchmark's, bypassing question synthesis.
    """
    from ranx import Qrels

    user_id = user_id or settings.system_user_id
    matrix = matrix or SweepMatrix()
    if gold is None:
        items = await build_questions(questions, user_id)
        gold = [qa for qa in items if qa.expected is not None]
    configs = build_matrix(matrix)
    if not gold:
        return SweepReport(n=0, k=k, results=[], comparison=None, best_label=None)
    metrics = [f"recall@{k}", f"ndcg@{k}", "mrr"]
    qrels = Qrels({f"q{index}": {"rel": 1} for index in range(len(gold))})
    measured = await measure_configs(configs, gold, user_id, k)
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
