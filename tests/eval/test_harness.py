import math
import uuid
from typing import NamedTuple, cast

import dbutil
import numpy as np
import pytest
from doubles import FakeLLM
from eval_util import install_constant_recall
from hypothesis import given
from hypothesis import strategies as st
from strategies import recall_results

import aizk.eval.harness as harness
from aizk.config import settings
from aizk.eval import (
    JUDGE_SYSTEM,
    QA,
    QUESTION_SYSTEM,
    EvalReport,
    GeneratedQuestion,
    JudgeVerdict,
    build_questions,
    judge_answerable,
    retrieved_scores,
    run_eval,
    sample_facts,
    significant_winner,
)
from aizk.eval.scale import CorpusScale, Generated, grow_corpus, purge_principal
from aizk.retrieval import RecallResult
from aizk.store import User, system_session


class RanxCase(NamedTuple):
    """A ranx compare-report dict shaped exactly as `significant_winner` reads it, with its inputs.

    data: the report dict carrying model_names, per-config scores, and pairwise p-values.
    current: the live config every other is tested against.
    metric: the metric the flip decision reads.
    max_p: the largest p-value a win may carry to count.
    names: the config labels.
    score: each config's metric score.
    pval: each config's p-value against current, sometimes NaN.
    """

    data: dict[str, object]
    current: str
    metric: str
    max_p: float
    names: list[str]
    score: dict[str, float]
    pval: dict[str, float]


@st.composite
def ranx_reports(draw: st.DrawFn) -> RanxCase:
    """Draw a report whose p-values straddle the ceiling and NaN, exercising every flip branch."""
    metric = "ndcg@8"
    indices = draw(
        st.lists(st.integers(min_value=0, max_value=50), min_size=1, max_size=5, unique=True)
    )
    names = [f"cfg{i}" for i in indices]
    current = draw(st.sampled_from(names))
    finite = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
    p_values = st.one_of(finite, st.just(float("nan")))
    score = {name: draw(finite) for name in names}
    pval = {name: draw(p_values) for name in names}
    data: dict[str, object] = {"model_names": names}
    for name in names:
        data[name] = {
            "scores": {metric: score[name]},
            "comparisons": {current: {metric: pval[name]}},
        }
    return RanxCase(data, current, metric, draw(finite), names, score, pval)


@given(case=ranx_reports())
def test_significant_winner_flips_only_on_a_significant_beating_config(case: RanxCase) -> None:
    """The flip signal names the top config that outscores current and clears the p ceiling."""
    valid = [
        name
        for name in case.names
        if name != case.current
        and case.score[name] > case.score[case.current]
        and not math.isnan(case.pval[name])
        and case.pval[name] <= case.max_p
    ]
    expected = max(valid, key=lambda name: case.score[name]) if valid else None

    assert significant_winner(case.data, case.current, case.metric, case.max_p) == expected


@st.composite
def scored_cases(draw: st.DrawFn) -> tuple[QA, RecallResult]:
    """A recall bundle paired with an expected fact that hits or misses, the hit-labeling probe.

    Covers the four ways `retrieved_scores` must read an expectation: no expectation, an absent
    one, an exact text, and a substring of one, so the property pins the rel-labeling and the
    descending rank scores over every shape rather than a few hand-picked rows.
    """
    result = draw(recall_results())
    texts = [fact.statement for fact in result.facts] + [hit.text for hit in result.hits]
    absent = "Z" * 64  # longer than any generated text, so it can never match
    kinds = ["none", "absent"] + (["exact", "substr"] if any(texts) else [])
    kind = draw(st.sampled_from(kinds))
    if kind == "none":
        expected: str | None = None
    elif kind == "absent":
        expected = absent
    elif kind == "exact":
        expected = draw(st.sampled_from(texts))
    else:
        base = draw(st.sampled_from([text for text in texts if text]))
        start = draw(st.integers(min_value=0, max_value=len(base) - 1))
        end = draw(st.integers(min_value=start + 1, max_value=len(base)))
        expected = base[start:end]
    return QA(question=draw(st.text(min_size=1, max_size=20)), expected=expected), result


@given(case=scored_cases())
def test_retrieved_scores_labels_the_first_expected_match_relevant(
    case: tuple[QA, RecallResult],
) -> None:
    """Exactly the first text carrying the expected fact is `rel`, the rest descend by rank."""
    qa, result = case
    texts = [fact.statement for fact in result.facts] + [hit.text for hit in result.hits]
    scores = retrieved_scores(qa, result)

    # one score per ranked text, each the rank-descending weight len(texts) - rank
    assert len(scores) == len(texts)
    assert sorted(scores.values()) == [
        float(len(texts) - rank) for rank in reversed(range(len(texts)))
    ]

    first_hit = next(
        (
            rank
            for rank, text in enumerate(texts)
            if qa.expected is not None and (text == qa.expected or qa.expected in text)
        ),
        None,
    )
    if first_hit is None:
        assert "rel" not in scores
    else:
        assert list(scores).count("rel") == 1
        assert scores["rel"] == float(len(texts) - first_hit)


@given(rerank=st.booleans(), ppr=st.booleans(), embed_dim=st.integers(min_value=1, max_value=4096))
def test_swept_settings_overlays_then_restores(rerank: bool, ppr: bool, embed_dim: int) -> None:
    """The overlay sets each field for the block and restores the previous value after it."""
    before = (settings.rerank, settings.ppr, settings.embed_dim)
    with harness.swept_settings(rerank=rerank, ppr=ppr, embed_dim=embed_dim):
        assert (settings.rerank, settings.ppr, settings.embed_dim) == (rerank, ppr, embed_dim)
    assert (settings.rerank, settings.ppr, settings.embed_dim) == before


def test_swept_settings_restores_even_when_the_block_raises() -> None:
    """A raise inside the block still restores every overlaid field, the finally guarantee."""
    before = settings.rerank
    with pytest.raises(RuntimeError), harness.swept_settings(rerank=not before):
        raise RuntimeError("boom")
    assert settings.rerank == before


def test_build_questions_wraps_caller_questions_as_judge_only_items() -> None:
    """A caller's questions become items with no gold, so the judge alone scores them, no DB."""
    items = dbutil.run(build_questions(["what holds", "what fell"], settings.system_user_id))

    assert [item.question for item in items] == ["what holds", "what fell"]
    assert all(item.expected is None for item in items)


# the source fact the synthesized question must answer without echoing its nouns, and a paraphrase
# that reaches for a description instead, the behavior QUESTION_SYSTEM instructs the model toward.
SOURCE_FACT = "The Leech lattice is optimal in dimension 24."
PARAPHRASE = "Which packing fills twenty four dimensional space most densely?"


def sent_messages(fake_llm: FakeLLM) -> list[dict[str, str]]:
    """The system-then-user message pair the first recorded `structured` turn assembled.

    fake_llm: the recording LLM whose first captured call's messages are read.
    """
    messages = fake_llm.completions.calls[0]["messages"]
    assert isinstance(messages, list)
    return cast(list[dict[str, str]], messages)


def test_build_questions_synthesizes_gold_from_sampled_facts(
    monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLLM
) -> None:
    """A null question set samples facts and paraphrases each into gold through QUESTION_SYSTEM."""
    fake_llm.register(GeneratedQuestion, GeneratedQuestion(question=PARAPHRASE))

    async def stub_sample_facts(principal_id: uuid.UUID, n: int) -> list[str]:
        return [SOURCE_FACT]

    monkeypatch.setattr(harness, "sample_facts", stub_sample_facts)

    items = dbutil.run(build_questions(None, settings.system_user_id))

    assert len(items) == 1
    assert items[0].expected == SOURCE_FACT
    assert items[0].question == PARAPHRASE and items[0].question != SOURCE_FACT
    messages = sent_messages(fake_llm)
    assert messages[0]["content"] == QUESTION_SYSTEM and messages[1]["content"] == SOURCE_FACT


@pytest.mark.parametrize("answerable", [True, False])
def test_judge_answerable_reads_the_verdict_off_the_llm(
    fake_llm: FakeLLM, answerable: bool
) -> None:
    """`judge_answerable` returns the verdict the judge gives over the question and the context."""
    fake_llm.register(JudgeVerdict, JudgeVerdict(answerable=answerable))

    verdict = dbutil.run(judge_answerable("is it so", "the context"))

    assert verdict is answerable
    system, user = sent_messages(fake_llm)
    assert system["content"] == JUDGE_SYSTEM
    assert user["content"].startswith("Question.") and "Context." in user["content"]
    assert "is it so" in user["content"] and "the context" in user["content"]


def test_run_eval_scores_a_half_hitting_gold_sweep_and_ab(monkeypatch: pytest.MonkeyPatch) -> None:
    """A half-hitting gold set scores hit-at-k 0.5, sweeps the toggles, and A/Bs routing."""
    gold = [
        QA(question="what does alpha hold", expected="alpha holds"),
        QA(question="what does beta hold", expected="beta holds"),
    ]

    async def stub_build_questions(
        questions: list[str] | None, principal_id: uuid.UUID
    ) -> list[QA]:
        return gold

    monkeypatch.setattr(harness, "build_questions", stub_build_questions)
    install_constant_recall(monkeypatch, harness, "alpha holds")

    report = dbutil.run(run_eval(questions=None, k=4))

    assert isinstance(report, EvalReport)
    assert report.n == 2
    assert report.hit_at_k == 0.5
    assert set(report.per_config) == {
        f"rerank={rerank},ppr={ppr}" for rerank, ppr in harness.TOGGLES
    }
    assert all(0.0 <= value <= 1.0 for value in report.per_config.values())
    assert report.comparison is not None  # the toggle sweep ran a ranx comparison
    # every toggle recalls the same fixed fact, so no config beats the baseline significantly
    assert report.significant_best is None and report.routing_winner is None
    assert report.fixed_hit_at_k == 0.5 and report.routed_hit_at_k == 0.5
    assert report.mean_judge is None


@pytest.mark.parametrize(
    ("questions", "gold", "judge", "exp_configs", "exp_judge"),
    [
        (None, [QA(question="q", expected="alpha holds")], True, True, 1.0),
        (["only ask"], None, True, False, 1.0),
        (["only ask"], None, False, False, None),
    ],
    ids=["gold+judge", "caller+judge", "caller+nojudge"],
)
def test_run_eval_judge_and_no_gold_paths(
    monkeypatch: pytest.MonkeyPatch,
    fake_llm: FakeLLM,
    questions: list[str] | None,
    gold: list[QA] | None,
    judge: bool,
    exp_configs: bool,
    exp_judge: float | None,
) -> None:
    """Judging runs only when enabled, and a caller's questions leave no gold to sweep or A/B."""
    fake_llm.register(JudgeVerdict, JudgeVerdict(answerable=True))
    monkeypatch.setattr(settings, "eval_judge", judge)
    install_constant_recall(monkeypatch, harness, "alpha holds")
    if gold is not None:

        async def stub_build_questions(
            questions: list[str] | None, principal_id: uuid.UUID
        ) -> list[QA]:
            return gold

        monkeypatch.setattr(harness, "build_questions", stub_build_questions)

    report = dbutil.run(run_eval(questions=questions, k=4))

    assert (report.per_config != {}) == exp_configs
    assert (report.comparison is not None) == exp_configs
    assert report.mean_judge == exp_judge
    if not exp_configs:
        # no gold means the sweep, the significance pick, and the routing A/B never ran
        assert report.hit_at_k == 0.0 and report.ndcg_at_k == 0.0 and report.mrr == 0.0
        assert report.fixed_hit_at_k is None and report.routing_winner is None


def test_sample_facts_returns_latest_statements_in_a_stable_id_order(migrated_db: None) -> None:
    """`sample_facts` surfaces up to n latest statements in id order, the auto-eval source pool."""

    async def body() -> None:
        await dbutil.reset_db()
        async with system_session() as session:
            principal_id = (await User.create(session, "eval-sample")).id
        try:
            await grow_corpus(
                principal_id, Generated(), CorpusScale.for_size(20), np.random.default_rng(0)
            )
            sampled = await sample_facts(principal_id, 5)
            again = await sample_facts(principal_id, 5)

            assert len(sampled) == 5
            assert all(isinstance(statement, str) for statement in sampled)
            assert sampled == again  # ordered by LiveFact.id, so the prefix is stable run to run
        finally:
            await purge_principal(principal_id)

    dbutil.run(body())
