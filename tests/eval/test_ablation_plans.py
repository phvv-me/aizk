from unittest.mock import AsyncMock

import dbutil
import pytest

import eval.plans as plans_module
from aizk.retrieval import Plan
from aizk.retrieval.models.lane import Lane
from aizk.store.identity import User
from eval.plans import (
    Arm,
    ArmScore,
    QueryResult,
    RetrievalBenchmark,
    Stratum,
    StudyQuestion,
    ranking_metrics,
)


def test_profile_lane_follows_the_plan_toggle() -> None:
    assert any(lane.kind is Lane.Kind.PROFILE for lane in Plan.focused().lanes)
    assert all(
        lane.kind is not Lane.Kind.PROFILE for lane in Plan.maximal_without_profiles().lanes
    )


def test_ir_measures_returns_native_per_query_values() -> None:
    question = StudyQuestion(
        question="q",
        expected=("first", "second"),
        stratum=Stratum.LOCAL,
    )

    rank, hit, ndcg, mrr = ranking_metrics(
        question,
        {"rel0": 2.0, "d1": 1.0},
        3,
    )
    empty = ranking_metrics(question, {}, 3)

    assert rank == 1
    assert hit == 1.0
    assert ndcg == pytest.approx(1.0 / (1.0 + 1.0 / 1.584962500721156))
    assert mrr == 1.0
    assert empty == (None, 0.0, 0.0, 0.0)


def test_retrieval_benchmark_uses_frozen_questions_and_retains_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = StudyQuestion(
        id="local:0000",
        question="q",
        expected=("e",),
        stratum=Stratum.LOCAL,
    )
    arm = Arm.production()
    row = QueryResult(
        arm=arm.name,
        stratum=Stratum.LOCAL,
        question_id=question.id,
        question=question.question,
        rank_first_relevant=1,
        hit_at_k=1.0,
        ndcg_at_k=1.0,
        mrr=1.0,
        judge=1.0,
        latency_ms=2.0,
    )
    score = ArmScore(
        arm=arm.name,
        hit_at_k=1.0,
        ndcg_at_k=1.0,
        mrr=1.0,
        judge=1.0,
        latency_p50_ms=2.0,
        rows=(row,),
    )
    measure = AsyncMock(return_value=score)
    monkeypatch.setattr(plans_module, "measure_arm", measure)
    user = User.system()

    report = dbutil.run(
        RetrievalBenchmark(
            user=user,
            k=3,
            strata=(Stratum.LOCAL,),
            questions=(question,),
            judge=True,
        ).run((arm,))
    )

    assert report.rows == (row,)
    measure.assert_awaited_once_with(arm, [question], user, 3, judge=True)
