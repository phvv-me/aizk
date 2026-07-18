import json
from pathlib import Path
from typing import Any, cast

import pytest
from benchmark import conftest as bench

from eval.plans import Arm, Stratum

_METRIC_VALUES = {
    "rank_first_relevant": 1.0,
    "hit_at_k": 1.0,
    "ndcg_at_k": 1.0,
    "mrr": 1.0,
    "judge": 1.0,
    "latency_ms": 1.0,
}


class _ForceRegenConfig:
    def getoption(self, name: str) -> bool:
        assert name == "--force-regen"
        return True


def _complete_rows() -> list[dict[str, object]]:
    return [
        {
            "arm": arm.name,
            "stratum": stratum.value,
            "question_id": f"{stratum.value}-question",
            **_METRIC_VALUES,
        }
        for arm in Arm.ablations()
        for stratum in Stratum
    ]


def _collector(rows: list[dict[str, object]]) -> bench.BenchmarkCollector:
    collector = bench.BenchmarkCollector(mode="gate")
    collector.rows = rows
    collector.fingerprint = "corpus-fingerprint"
    return collector


def test_benchmark_collector_validates_complete_result_sets() -> None:
    complete = _complete_rows()
    _collector(complete).validate_complete()

    missing_arm = next(iter(Arm.ablations())).name
    missing_stratum = next(iter(Stratum)).value
    cases = (
        ([], "no rows collected"),
        (
            [row for row in complete if row["arm"] != missing_arm],
            "unexpected arms",
        ),
        (
            [row for row in complete if row["stratum"] != missing_stratum],
            "unexpected strata",
        ),
        (complete[1:], "identical question keys"),
        (
            [*complete, dict(complete[0])],
            "duplicate arm/stratum/question_id keys",
        ),
    )
    for rows, message in cases:
        with pytest.raises(pytest.UsageError, match=message):
            _collector(rows).validate_complete()


def test_incomplete_results_do_not_mutate_baselines_or_publish_reports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    artifacts = tmp_path / "artifacts"
    monkeypatch.setattr(bench, "_DATA", data)
    monkeypatch.setattr(bench, "_BASELINE", data / "retrieval_baseline.csv")
    monkeypatch.setattr(bench, "_METADATA", data / "retrieval_baseline.meta.json")
    monkeypatch.setattr(bench, "_ARTIFACTS", artifacts)

    missing_arm = next(iter(Arm.ablations())).name
    rows = [row for row in _complete_rows() if row["arm"] != missing_arm]
    collector = _collector(rows)

    class RecordingRegression:
        called = False

        def check(self, data_frame: Any, *, fullpath: Path) -> None:
            del data_frame, fullpath
            self.called = True

    regression = RecordingRegression()
    with pytest.raises(pytest.UsageError, match="unexpected arms"):
        collector.gate(
            regression,
            cast(pytest.Config, _ForceRegenConfig()),
        )
    assert not regression.called
    assert not data.exists()

    with pytest.raises(pytest.UsageError, match="unexpected arms"):
        bench._write_report(collector, None)
    assert not artifacts.exists()


def test_metadata_is_replaced_only_after_successful_baseline_regeneration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    baseline = data / "retrieval_baseline.csv"
    metadata = data / "retrieval_baseline.meta.json"
    temporary_metadata = metadata.with_name(f"{metadata.name}.tmp")
    data.mkdir()
    metadata.write_text("old metadata\n", encoding="utf-8")
    monkeypatch.setattr(bench, "_DATA", data)
    monkeypatch.setattr(bench, "_BASELINE", baseline)
    monkeypatch.setattr(bench, "_METADATA", metadata)

    collector = _collector(_complete_rows())
    config = cast(pytest.Config, _ForceRegenConfig())

    class FailingRegression:
        def check(self, data_frame: Any, *, fullpath: Path) -> None:
            del data_frame, fullpath
            raise RuntimeError("regeneration failed")

    with pytest.raises(RuntimeError, match="regeneration failed"):
        collector.gate(FailingRegression(), config)
    assert metadata.read_text(encoding="utf-8") == "old metadata\n"
    assert not temporary_metadata.exists()

    class SuccessfulRegression:
        def check(self, data_frame: Any, *, fullpath: Path) -> None:
            del data_frame
            assert fullpath == baseline
            assert metadata.read_text(encoding="utf-8") == "old metadata\n"

    collector.gate(SuccessfulRegression(), config)
    assert json.loads(metadata.read_text(encoding="utf-8")) == collector.metadata()
    assert not temporary_metadata.exists()
