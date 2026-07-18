from collections import Counter
from pathlib import Path
from typing import Any

import dbutil
import pytest

from eval.corpus import load_frozen_corpus
from eval.plans import Arm, RetrievalBenchmark, Stratum

pytestmark = pytest.mark.benchmark

_CORPUS = Path(__file__).parent / "data" / "retrieval_questions.jsonl"


@pytest.mark.parametrize("arm", Arm.ablations(), ids=lambda arm: arm.name)
def test_retrieval_ablation_arm(
    arm: Arm,
    benchmark_collector: Any,
    migrated_db: None,
) -> None:
    del migrated_db
    corpus = load_frozen_corpus(_CORPUS)
    counts = Counter(question.stratum for question in corpus.questions)
    assert set(counts) == set(Stratum)
    assert min(counts.values()) == max(counts.values())
    report = dbutil.run(
        RetrievalBenchmark(
            k=benchmark_collector.k,
            strata=tuple(Stratum),
            questions=corpus.questions,
            judge=True,
        ).run(
            arms=(arm,),
            title=f"retrieval ablation: {arm.name}",
        )
    )

    assert len(report.rows) == len(corpus.questions)
    benchmark_collector.add(report, corpus.fingerprint)


def test_retrieval_ablation_gate(
    benchmark_collector: Any,
    dataframe_regression: Any,
    eval_mode: str,
    pytestconfig: pytest.Config,
) -> None:
    if eval_mode == "gate":
        benchmark_collector.gate(dataframe_regression, pytestconfig)
