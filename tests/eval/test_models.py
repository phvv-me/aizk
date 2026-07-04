from hypothesis import given
from hypothesis import strategies as st

from aizk.eval import QA, EvalReport, GeneratedQuestion, JudgeVerdict

_scores = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


@given(
    n=st.integers(min_value=0, max_value=1000),
    hit=_scores,
    ndcg=_scores,
    mrr=_scores,
    mean_judge=st.none() | _scores,
    per_config=st.dictionaries(st.text(max_size=12), _scores, max_size=4),
    comparison=st.none() | st.text(max_size=20),
    best=st.none() | st.text(max_size=12),
    fixed=st.none() | _scores,
    routed=st.none() | _scores,
    winner=st.none() | st.sampled_from(["routed", "fixed"]),
)
def test_eval_report_round_trips_through_its_own_serialization(
    n: int,
    hit: float,
    ndcg: float,
    mrr: float,
    mean_judge: float | None,
    per_config: dict[str, float],
    comparison: str | None,
    best: str | None,
    fixed: float | None,
    routed: float | None,
    winner: str | None,
) -> None:
    """Every scorecard field survives a dump then load unchanged, our field wiring intact."""
    report = EvalReport(
        n=n,
        hit_at_k=hit,
        ndcg_at_k=ndcg,
        mrr=mrr,
        mean_judge=mean_judge,
        per_config=per_config,
        comparison=comparison,
        significant_best=best,
        fixed_hit_at_k=fixed,
        routed_hit_at_k=routed,
        routing_winner=winner,
    )

    assert EvalReport.model_validate(report.model_dump()) == report


def test_optional_ab_fields_default_to_null_on_a_report_without_gold() -> None:
    """A report built without the A/B fields leaves them null, the no-gold default."""
    report = EvalReport(
        n=0,
        hit_at_k=0.0,
        ndcg_at_k=0.0,
        mrr=0.0,
        mean_judge=None,
        per_config={},
        comparison=None,
        significant_best=None,
    )

    assert report.fixed_hit_at_k is None
    assert report.routed_hit_at_k is None
    assert report.routing_winner is None


def test_qa_and_verdict_models_carry_their_fields() -> None:
    """The small eval items hold exactly the fields the harness reads off them."""
    qa = QA(question="what holds", expected=None)
    generated = GeneratedQuestion(question="which packing is densest")
    verdict = JudgeVerdict(answerable=True)

    assert (qa.question, qa.expected) == ("what holds", None)
    assert generated.question == "which packing is densest"
    assert verdict.answerable is True
