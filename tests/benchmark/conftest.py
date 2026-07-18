import io
import json
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import pandas as pd
import pytest
from autorank import autorank, create_report
from rich.console import Console
from rich.table import Table

from aizk.config import settings
from eval.plans import Arm, PlanStudyReport, Stratum
from eval.stats import (
    holm_rejections,
    paired_cohens_dz,
    paired_permutation_pvalue,
    signal_to_noise,
)

_ROOT = Path(__file__).parent
_DATA = _ROOT / "data"
_ARTIFACTS = Path("artifacts/eval")
_BASELINE = _DATA / "retrieval_baseline.csv"
_METADATA = _DATA / "retrieval_baseline.meta.json"
_METRICS = (
    "rank_first_relevant",
    "hit_at_k",
    "ndcg_at_k",
    "mrr",
    "judge",
    "latency_ms",
)
_HIGHER_BETTER = frozenset({"hit_at_k", "ndcg_at_k", "mrr", "judge"})
_MINIMUM_DZ = 0.2


class DataFrameRegression(Protocol):
    """The pytest-regressions dataframe fixture boundary."""

    def check(self, data_frame: pd.DataFrame, *, fullpath: Path) -> None: ...


class TerminalWriter(Protocol):
    """The pytest terminal reporter boundary the report writer needs."""

    def write(self, content: str) -> None: ...


@dataclass
class BenchmarkCollector:
    """Session-owned per-query rows and their immutable run metadata."""

    mode: str
    k: int = 8
    rows: list[dict[str, object]] = field(default_factory=list)
    fingerprint: str | None = None

    def add(self, report: PlanStudyReport, fingerprint: str) -> None:
        """Collect one parametrized arm report."""
        if self.fingerprint is not None and self.fingerprint != fingerprint:
            raise pytest.UsageError("benchmark arms loaded different frozen corpora")
        self.fingerprint = fingerprint
        self.rows.extend(row.model_dump(mode="json") for row in report.rows)

    def validate_complete(self) -> None:
        """Reject incomplete or ambiguous benchmark result sets."""
        if not self.rows:
            raise pytest.UsageError("benchmark results are incomplete: no rows collected")
        frame = self.frame()
        duplicate_rows = frame.loc[
            frame.duplicated(["arm", "stratum", "question_id"], keep=False),
            ["arm", "stratum", "question_id"],
        ].drop_duplicates()
        if not duplicate_rows.empty:
            raise pytest.UsageError(
                "benchmark results contain duplicate arm/stratum/question_id keys: "
                f"{duplicate_rows.to_dict(orient='records')}"
            )
        expected_arms = {arm.name for arm in Arm.ablations()}
        actual_arms = set(frame["arm"].astype(str))
        if actual_arms != expected_arms:
            raise pytest.UsageError(
                "benchmark results have unexpected arms: "
                f"expected {sorted(expected_arms)}, got {sorted(actual_arms)}"
            )
        expected_strata = {stratum.value for stratum in Stratum}
        actual_strata = set(frame["stratum"].astype(str))
        if actual_strata != expected_strata:
            raise pytest.UsageError(
                "benchmark results have unexpected strata: "
                f"expected {sorted(expected_strata)}, got {sorted(actual_strata)}"
            )
        question_keys = {
            str(arm): frozenset(
                (str(row.stratum), str(row.question_id)) for row in group.itertuples(index=False)
            )
            for arm, group in frame.groupby("arm", sort=False)
        }
        reference_arm = min(expected_arms)
        mismatched_arms = sorted(
            arm for arm, keys in question_keys.items() if keys != question_keys[reference_arm]
        )
        if mismatched_arms:
            raise pytest.UsageError(
                "benchmark arms do not have identical question keys: "
                f"{mismatched_arms} differ from {reference_arm}"
            )

    def frame(self) -> pd.DataFrame:
        """Return sorted tidy long-format observations."""
        return (
            pd.DataFrame(self.rows)
            .sort_values(["arm", "stratum", "question_id"])
            .reset_index(drop=True)
        )

    def metric_frame(self) -> pd.DataFrame:
        """Return metrics with misses represented below the retrieval cutoff."""
        frame = self.frame()
        frame["rank_first_relevant"] = frame["rank_first_relevant"].fillna(self.k + 1)
        return frame

    def regression_frame(self) -> pd.DataFrame:
        """Return the numeric indexed frame accepted by dataframe_regression."""
        return (
            self.metric_frame()
            .set_index(["arm", "stratum", "question_id"])[list(_METRICS)]
            .sort_index()
        )

    def metadata(self) -> dict[str, object]:
        """Return the identity fields that make a baseline comparable."""
        frame = self.frame()
        return {
            "schema": 1,
            "corpus_sha256": self.fingerprint,
            "judge_model": settings.llm_model,
            "k": self.k,
            "arms": frame["arm"].drop_duplicates().tolist(),
        }

    def gate(
        self,
        regression: DataFrameRegression,
        config: pytest.Config,
    ) -> None:
        """Bless explicitly or fail only for meaningful significant regressions."""
        self.validate_complete()
        current = self.regression_frame()
        _DATA.mkdir(parents=True, exist_ok=True)
        if config.getoption("--force-regen"):
            regression.check(current, fullpath=_BASELINE)
            temporary_metadata = _METADATA.with_name(f"{_METADATA.name}.tmp")
            temporary_metadata.write_text(
                f"{json.dumps(self.metadata(), indent=2, sort_keys=True)}\n",
                encoding="utf-8",
            )
            temporary_metadata.replace(_METADATA)
            return
        if not _BASELINE.exists() or not _METADATA.exists():
            raise pytest.UsageError(
                "retrieval baseline missing, rerun gate mode with --force-regen"
            )
        expected_metadata = json.loads(_METADATA.read_text(encoding="utf-8"))
        if expected_metadata != self.metadata():
            raise pytest.UsageError(
                "retrieval baseline metadata does not match this run: "
                f"expected {expected_metadata}, got {self.metadata()}"
            )
        baseline = pd.read_csv(
            _BASELINE,
            index_col=["arm", "stratum", "question_id"],
        ).sort_index()
        if list(baseline.columns) != list(current.columns) or not baseline.index.equals(
            current.index
        ):
            raise pytest.UsageError("retrieval baseline rows do not match this run")
        failures = _gate_failures(current, baseline)
        if failures:
            pytest.fail(
                "Holm-significant retrieval regressions with dz >= "
                f"{_MINIMUM_DZ}:\n" + "\n".join(failures),
                pytrace=False,
            )


_COLLECTOR_KEY = pytest.StashKey[BenchmarkCollector]()


@pytest.fixture(scope="session")
def benchmark_collector(
    pytestconfig: pytest.Config,
    eval_mode: str,
) -> BenchmarkCollector:
    """Install the session collector used by parametrized benchmark tests."""
    collector = BenchmarkCollector(mode=eval_mode)
    pytestconfig.stash[_COLLECTOR_KEY] = collector
    return collector


def _pivot(frame: pd.DataFrame, metric: str) -> pd.DataFrame:
    return frame.pivot(
        index=["stratum", "question_id"],
        columns="arm",
        values=metric,
    ).dropna()


def _ablation_analysis(frame: pd.DataFrame) -> pd.DataFrame:
    comparisons: list[dict[str, float | str | bool]] = []
    for metric in _METRICS:
        pivot = _pivot(frame, metric)
        if "maximal" not in pivot:
            continue
        for arm in pivot.columns:
            if arm == "maximal":
                continue
            maximal = pivot["maximal"].to_numpy(dtype=float)
            ablated = pivot[arm].to_numpy(dtype=float)
            if metric in _HIGHER_BETTER:
                first, second, conclusion = maximal, ablated, "maximal better"
            elif metric == "latency_ms":
                first, second, conclusion = maximal, ablated, "maximal slower"
            else:
                first, second, conclusion = ablated, maximal, "maximal better"
            comparisons.append(
                {
                    "arm": arm,
                    "metric": metric,
                    "mean_delta": float(first.mean() - second.mean()),
                    "pvalue": paired_permutation_pvalue(
                        first,
                        second,
                        alternative="greater",
                    ),
                    "dz": paired_cohens_dz(first, second),
                    "conclusion": conclusion,
                }
            )
    rejected = (
        holm_rejections([float(row["pvalue"]) for row in comparisons]) if comparisons else ()
    )
    for row, significant in zip(comparisons, rejected, strict=True):
        row["holm_significant"] = significant
    return pd.DataFrame(comparisons)


def _snr_table(frame: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, float | str]] = []
    for metric in _METRICS:
        pivot = _pivot(frame, metric)
        if pivot.shape[0] < 2 or pivot.shape[1] < 2:
            continue
        diagnostic = signal_to_noise([pivot[arm].to_numpy(dtype=float) for arm in pivot.columns])
        rows.append({"metric": metric, **diagnostic.model_dump()})
    return pd.DataFrame(rows)


def _autorank_text(frame: pd.DataFrame) -> str:
    pivot = _pivot(frame, "ndcg_at_k")
    if pivot.shape[0] < 5 or pivot.shape[1] < 2:
        return "autorank unavailable: insufficient paired observations"
    stream = io.StringIO()
    with redirect_stdout(stream):
        ranked = autorank(
            pivot,
            alpha=0.05,
            verbose=False,
            order="descending",
            force_mode="nonparametric",
        )
        created = create_report(ranked)
        if created is not None:
            print(created)
    return stream.getvalue().strip()


def _rich_table(frame: pd.DataFrame) -> str:
    summary = (
        frame.groupby(["stratum", "arm"], as_index=False)[list(_METRICS)]
        .mean()
        .sort_values(["stratum", "arm"])
    )
    table = Table(title="Aizk retrieval ablation")
    table.add_column("stratum")
    table.add_column("arm")
    for metric in _METRICS:
        table.add_column(metric, justify="right")
    for row in summary.itertuples(index=False):
        table.add_row(
            str(row.stratum),
            str(row.arm),
            *(f"{float(getattr(row, metric)):.4f}" for metric in _METRICS),
        )
    output = io.StringIO()
    Console(file=output, force_terminal=False, width=160).print(table)
    return output.getvalue()


def _gate_failures(
    current: pd.DataFrame,
    baseline: pd.DataFrame,
) -> list[str]:
    comparisons: list[dict[str, float | str]] = []
    pairs = sorted({(str(index[0]), str(index[1])) for index in current.index})
    for arm, stratum in pairs:
        current_group = current.xs((arm, stratum), level=("arm", "stratum"))
        baseline_group = baseline.xs((arm, stratum), level=("arm", "stratum"))
        for metric in _METRICS:
            current_values = current_group[metric].to_numpy(dtype=float)
            baseline_values = baseline_group[metric].to_numpy(dtype=float)
            if metric in _HIGHER_BETTER:
                first, second = baseline_values, current_values
            else:
                first, second = current_values, baseline_values
            comparisons.append(
                {
                    "arm": arm,
                    "stratum": stratum,
                    "metric": metric,
                    "pvalue": paired_permutation_pvalue(
                        first,
                        second,
                        alternative="greater",
                    ),
                    "dz": paired_cohens_dz(first, second),
                }
            )
    rejected = holm_rejections([float(row["pvalue"]) for row in comparisons])
    return [
        (
            f"{row['arm']} {row['stratum']} {row['metric']} "
            f"p={float(row['pvalue']):.6g} dz={float(row['dz']):.3f}"
        )
        for row, significant in zip(comparisons, rejected, strict=True)
        if significant and float(row["dz"]) >= _MINIMUM_DZ
    ]


def _write_report(collector: BenchmarkCollector, terminal: TerminalWriter | None) -> None:
    collector.validate_complete()
    _ARTIFACTS.mkdir(parents=True, exist_ok=True)
    frame = collector.metric_frame()
    frame.to_csv(_ARTIFACTS / "retrieval-ablation-long.csv", index=False)
    summary = (
        frame.groupby(["stratum", "arm"], as_index=False)[list(_METRICS)]
        .mean()
        .sort_values(["stratum", "arm"])
    )
    ablations = _ablation_analysis(frame)
    snr = _snr_table(frame)
    report = (
        "# Aizk retrieval ablation\n\n"
        f"```json\n{json.dumps(collector.metadata(), indent=2, sort_keys=True)}\n```\n\n"
        "## Means\n\n"
        f"{summary.to_markdown(index=False)}\n\n"
        "## Paired ablations\n\n"
        f"{ablations.to_markdown(index=False)}\n\n"
        "## Signal to noise\n\n"
        f"{snr.to_markdown(index=False)}\n\n"
        "## Autorank nDCG report\n\n"
        f"{_autorank_text(frame)}\n"
    )
    (_ARTIFACTS / "retrieval-ablation.md").write_text(report, encoding="utf-8")
    if terminal is not None:
        terminal.write(_rich_table(frame))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Write report-mode artifacts after every parametrized arm has completed."""
    del exitstatus
    collector = session.config.stash.get(_COLLECTOR_KEY, None)
    if collector is None or collector.mode != "report" or not collector.rows:
        return
    _write_report(
        collector,
        session.config.pluginmanager.get_plugin("terminalreporter"),
    )
