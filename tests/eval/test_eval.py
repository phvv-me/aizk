import asyncio
import math
import uuid
from datetime import datetime
from typing import NamedTuple

import numpy as np
import pytest
from factories import FactHitFactory, RecallResultFactory
from graphdb import create_principal
from hypothesis import given
from hypothesis import strategies as st
from strategies import recall_results

import aizk.eval.harness as eval_module
from aizk.cli import migrate
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
from aizk.extract.llm import triples as llm_module
from aizk.retrieval import RecallResult


def fact_result(query: str, statement: str) -> RecallResult:
    """A recall bundle carrying one fact, the surface run_eval scores a hit against.

    query: the question the bundle answers.
    statement: the single fact statement the bundle surfaces.
    """
    return RecallResultFactory.build(
        query=query,
        hits=[],
        facts=[FactHitFactory.build(statement=statement)],
        communities=[],
    )


# the LLM seam doubles, recording each parse call and returning one fixed schema instance, the
# only mocking the harness tests do, standing in for the external Ollama OpenAI endpoint and never
# for any of our own classes


class CapturingMessage:
    """The `.choices[0].message` shape `structured` reads its `.parsed` reply off of."""

    def __init__(self, parsed: GeneratedQuestion | JudgeVerdict) -> None:
        self.parsed = parsed


class CapturingChoice:
    """The `.choices[0]` shape wrapping the capturing message, mirroring `ParsedChoice`."""

    def __init__(self, message: CapturingMessage) -> None:
        self.message = message


class CapturingCompletion:
    """A minimal stand-in for `openai.types.chat.ParsedChatCompletion`."""

    def __init__(self, choice: CapturingChoice) -> None:
        self.choices = [choice]


class CapturingCompletions:
    """A completions stand-in that records each prompt and returns one fixed reply.

    reply: the schema instance every parse call resolves to.
    calls: the keyword arguments of every parse call, captured for assertion.
    """

    def __init__(self, reply: GeneratedQuestion | JudgeVerdict) -> None:
        self.reply = reply
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> CapturingCompletion:
        """Record the call's arguments and return the fixed reply.

        kwargs: the model, response_format, and messages the harness passed.
        """
        self.calls.append(kwargs)
        return CapturingCompletion(CapturingChoice(CapturingMessage(self.reply)))


class CapturingChat:
    """The chat namespace wrapping the capturing completions.

    completions: the capturing completions endpoint.
    """

    def __init__(self, completions: CapturingCompletions) -> None:
        self.completions = completions


class CapturingClient:
    """An AsyncOpenAI client stand-in exposing the chat.completions.parse path the harness uses.

    completions: the capturing completions the chat namespace exposes.
    """

    def __init__(self, completions: CapturingCompletions) -> None:
        self.chat = CapturingChat(completions)


class RanxCase(NamedTuple):
    """A ranx compare-report dict shaped exactly as significant_winner reads it, with its inputs.

    data: the report dict carrying model_names, per-config scores, and pairwise p-values.
    current: the live config every other is tested against.
    metric: the metric the flip decision reads.
    max_p: the largest p-value a win may carry to count.
    names: the config labels, kept typed so the property recomputes the winner without re-reading.
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
def test_significant_winner_only_flips_on_a_significant_beating_config(case: RanxCase) -> None:
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

    Covers the four ways retrieved_scores must read an expectation, no expectation, an absent one,
    an exact text, and a substring of one, so the property pins the rel-labeling and the descending
    rank scores over every shape rather than three hand-picked rows.
    """
    result = draw(recall_results())
    texts = [fact.statement for fact in result.facts] + [hit.text for hit in result.hits]
    absent = "Z" * 64  # longer than any generated text, so it can never match
    kinds = ["none", "absent"] + (["exact", "substr"] if texts else [])
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
    """Exactly the first text that carries the expected fact is rel, the rest descend by rank."""
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


def test_build_questions_wraps_caller_questions_as_judge_only_items() -> None:
    """A caller's questions become items with no gold, so the judge alone scores them, no DB."""
    items = asyncio.run(build_questions(["what holds", "what fell"], settings.system_principal_id))

    assert [item.question for item in items] == ["what holds", "what fell"]
    assert all(item.expected is None for item in items)


# the source fact the synthesized question must answer without echoing its nouns, and a paraphrase
# that reaches for a description instead, the behavior QUESTION_SYSTEM instructs the model toward.
SOURCE_FACT = "The Leech lattice is optimal in dimension 24."
PARAPHRASE = "Which packing fills twenty four dimensional space the most densely?"


def test_build_questions_paraphrases_a_fact_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthesized questions carry the source fact as gold yet ask it through QUESTION_SYSTEM."""
    completions = CapturingCompletions(GeneratedQuestion(question=PARAPHRASE))
    monkeypatch.setattr(llm_module, "client_for", lambda *_, **__: CapturingClient(completions))

    async def stub_sample_facts(principal_id: uuid.UUID, n: int) -> list[str]:
        return [SOURCE_FACT]

    monkeypatch.setattr(eval_module, "sample_facts", stub_sample_facts)

    items = asyncio.run(build_questions(None, settings.system_principal_id))

    assert len(items) == 1
    assert items[0].expected == SOURCE_FACT
    assert items[0].question == PARAPHRASE and items[0].question != SOURCE_FACT
    messages = completions.calls[0]["messages"]
    assert isinstance(messages, list)
    assert messages[0]["content"] == QUESTION_SYSTEM
    assert messages[1]["content"] == SOURCE_FACT
    assert "verbatim" in QUESTION_SYSTEM


def test_run_eval_scores_a_half_hitting_gold_sweep_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_eval reports a hit-at-k in zero to one over a half-hitting gold set, fully offline."""
    gold = [
        QA(question="what does alpha hold", expected="alpha holds"),
        QA(question="what does beta hold", expected="beta holds"),
    ]

    async def stub_build_questions(
        questions: list[str] | None, principal_id: uuid.UUID
    ) -> list[QA]:
        return gold

    async def stub_recall(
        query: str,
        principal_id: uuid.UUID | None = None,
        k: int = 8,
        as_of: datetime | None = None,
    ) -> RecallResult:
        return fact_result(query, "alpha holds")

    monkeypatch.setattr(eval_module, "build_questions", stub_build_questions)
    monkeypatch.setattr(eval_module, "recall", stub_recall)

    report = asyncio.run(run_eval(questions=None, k=4))

    assert isinstance(report, EvalReport)
    assert report.n == 2
    assert report.hit_at_k == 0.5
    assert all(0.0 <= value <= 1.0 for value in report.per_config.values())
    assert report.comparison is not None  # the toggle sweep ran a ranx comparison
    assert report.routing_winner is None  # the stub ignores routing, so a tie never flips it
    assert report.mean_judge is None


def test_run_eval_judges_caller_questions_when_there_is_no_gold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller questions leave no gold, so the sweep is skipped and the judge alone scores it."""
    completions = CapturingCompletions(JudgeVerdict(answerable=True))
    monkeypatch.setattr(llm_module, "client_for", lambda *_, **__: CapturingClient(completions))

    async def stub_recall(
        query: str,
        principal_id: uuid.UUID | None = None,
        k: int = 8,
        as_of: datetime | None = None,
    ) -> RecallResult:
        return fact_result(query, "alpha holds")

    monkeypatch.setattr(eval_module, "recall", stub_recall)

    monkeypatch.setattr(settings, "eval_judge", True)
    report = asyncio.run(run_eval(questions=["what holds", "what fell"], k=4))

    assert report.n == 2
    assert report.per_config == {}  # no gold, so the toggle sweep never scored
    assert report.comparison is None
    assert report.mean_judge == 1.0
    # the judge asks the LLM over the question and the rendered context, our prompt assembly
    judge_message = completions.calls[0]["messages"]
    assert isinstance(judge_message, list)
    assert judge_message[0]["content"] == JUDGE_SYSTEM
    assert judge_message[1]["content"].startswith("Question.")
    assert "Context." in judge_message[1]["content"]


def test_judge_answerable_reads_the_verdict_off_the_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """judge_answerable returns the verdict the judge gives over the question and the context."""
    completions = CapturingCompletions(JudgeVerdict(answerable=False))
    monkeypatch.setattr(llm_module, "client_for", lambda *_, **__: CapturingClient(completions))

    verdict = asyncio.run(judge_answerable("is it so", "the context"))

    assert verdict is False
    message = completions.calls[0]["messages"]
    assert isinstance(message, list)
    assert "is it so" in message[1]["content"]
    assert "the context" in message[1]["content"]


def test_sample_facts_returns_latest_statements_in_a_stable_id_order(requires_db: None) -> None:
    """sample_facts surfaces up to n latest statements in id order, the auto-eval source pool."""

    async def scenario() -> None:
        migrate()
        principal_id = await create_principal("eval-sample", kind="system")
        try:
            await grow_corpus(
                principal_id,
                Generated(),
                CorpusScale.for_size(20),
                np.random.default_rng(0),
            )
            sampled = await sample_facts(principal_id, 5)
            again = await sample_facts(principal_id, 5)

            assert len(sampled) == 5
            assert all(isinstance(statement, str) for statement in sampled)
            assert sampled == again  # ordered by LiveFact.id, so the prefix is stable run to run
        finally:
            await purge_principal(principal_id)

    asyncio.run(scenario())
