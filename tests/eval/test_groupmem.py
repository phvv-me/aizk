import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import dbutil
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic_ai.models.test import TestModel
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

import aizk.eval.runner as runner_module
from aizk.eval import (
    BenchmarkAnswer,
    BenchmarkCorpusError,
    BenchmarkCorpusState,
    BenchmarkDataset,
    BenchmarkMessage,
    BenchmarkQuestion,
    BenchmarkRunner,
    FAMAScore,
    GroupMemBench,
    QuestionKind,
)
from aizk.extract.ingest import TextSource
from aizk.retrieval import Candidate
from aizk.store.identity import User
from aizk.types import Scopes


def groupmem_fixture(root: Path) -> None:
    data = root / "data" / "final" / "Finance"
    questions = root / "questions" / "Finance"
    data.mkdir(parents=True)
    questions.mkdir(parents=True)
    corpus = {
        "Risk / Team": [
            {
                "msg_node": "Msg_2",
                "content": "Later &amp; corrected",
                "author": "User_2",
                "role": "Reviewer",
                "timestamp": "2025-07-02T00:00:00Z",
                "reply_to": "Msg_1",
                "phase_name": "Review",
                "topic": "Risk",
                "is_noise": False,
                "is_decision_point": True,
            }
        ],
        "Planning": [
            {
                "msg_node": "Msg_1",
                "content": "Earlier plan",
                "author": "User_1",
                "role": "Lead",
                "timestamp": "2025-07-01T00:00:00Z",
            }
        ],
    }
    (data / "synthetic_domain_channels_rolevariants_Finance.json").write_text(
        json.dumps(corpus), encoding="utf-8"
    )
    rows = {
        QuestionKind.temporal: {
            "id": "temporal_1",
            "question": "What changed later?",
            "answer": "The plan was corrected.",
            "asking_user_id": "User_1",
        },
        QuestionKind.abstention: {
            "id": "abstention_1",
            "question": "What was never discussed?",
            "answer": "There is no information available.",
            "asking_user_id": "User_2",
        },
    }
    for kind, row in rows.items():
        (questions / f"{kind.value}.jsonl").write_text(f"{json.dumps(row)}\n", encoding="utf-8")


def sample_dataset(sampled_questions: bool = False) -> BenchmarkDataset:
    dataset = BenchmarkDataset(
        name="GroupMemBench",
        domain="Finance",
        fingerprint="fixture-corpus",
        messages=(
            BenchmarkMessage(
                id="Msg/1",
                content="The team selected the current plan.",
                author="User_1",
                role="Lead",
                timestamp=datetime(2025, 7, 1, tzinfo=UTC),
                channel="Risk / Team",
            ),
        ),
        questions=(
            BenchmarkQuestion(
                id="q1",
                question="What did I select?",
                answer="The current plan.",
                asking_user="User_1",
                kind=QuestionKind.user_implicit,
            ),
            BenchmarkQuestion(
                id="q2",
                question="What was absent?",
                answer="No information.",
                asking_user="User_2",
                kind=QuestionKind.abstention,
            ),
        ),
        sampled_questions=sampled_questions,
    )
    return dataset


def test_groupmem_validates_order_metadata_limits_and_fingerprint(tmp_path: Path) -> None:
    groupmem_fixture(tmp_path)
    source = GroupMemBench(root=tmp_path)
    dataset = source.load(
        "Finance",
        kinds=(QuestionKind.temporal, QuestionKind.abstention),
    )
    partial = source.load(
        "Finance",
        kinds=(QuestionKind.temporal,),
        message_limit=1,
        question_limit=1,
    )

    assert [message.id for message in dataset.messages] == ["Msg_1", "Msg_2"]
    assert dataset.messages[1].content == "Later & corrected"
    assert dataset.messages[1].reply_to == "Msg_1" and dataset.messages[1].decision
    assert [question.kind for question in dataset.questions] == [
        QuestionKind.temporal,
        QuestionKind.abstention,
    ]
    assert dataset.questions[1].should_abstain
    assert partial.fingerprint != dataset.fingerprint
    assert not partial.complete_corpus and partial.sampled_questions
    with pytest.raises(ValueError, match="nonnegative"):
        source.load("Finance", message_limit=-1)
    assert not source.load("Finance", kinds=(QuestionKind.temporal,), question_limit=0).questions


def runner(
    evaluator: Evaluator[BenchmarkQuestion, BenchmarkAnswer, None] | None = None,
) -> BenchmarkRunner:
    return BenchmarkRunner(
        TestModel(
            custom_output_args={"answer": "The current plan.", "abstained": False},
            model_name="answer-test",
        ),
        TestModel(
            custom_output_args={"reason": "matches", "pass": True, "score": 1.0},
            model_name="judge-test",
        ),
        evaluator=evaluator,
        progress=False,
    )


def test_prepare_batches_authored_sources_and_verifies_the_exact_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_sources: list[TextSource] = []
    built_scopes: list[Scopes] = []

    async def ingest(user: User, sources: list[TextSource]) -> list[uuid.UUID]:
        captured_sources.extend(sources)
        return [uuid.uuid7() for _ in sources]

    async def build(scopes: Scopes) -> tuple[int, int]:
        built_scopes.append(scopes)
        return 1, 1

    async def state(self: BenchmarkRunner, dataset: BenchmarkDataset) -> BenchmarkCorpusState:
        return BenchmarkCorpusState(documents=len(dataset.messages), pending_chunks=0)

    monkeypatch.setattr(runner_module, "ingest_texts", ingest)
    monkeypatch.setattr(runner_module, "build_graph", build)
    monkeypatch.setattr(BenchmarkRunner, "corpus_state", state)
    dataset = sample_dataset()
    benchmark = runner()

    assert dbutil.run(benchmark.prepare(dataset)) == 1
    [source] = captured_sources
    assert source.created_by not in benchmark.scope(dataset)
    assert source.scopes == benchmark.scope(dataset)
    assert source.capture and source.capture.speaker_label == "User_1"
    assert source.source_uri and source.source_uri.startswith(
        "groupmembench://fixture-corpus/Finance/"
    )
    assert built_scopes == [benchmark.scope(dataset)]

    async def missing(self: BenchmarkRunner, dataset: BenchmarkDataset) -> BenchmarkCorpusState:
        return BenchmarkCorpusState(documents=0, pending_chunks=1)

    monkeypatch.setattr(BenchmarkRunner, "corpus_state", missing)
    with pytest.raises(BenchmarkCorpusError, match="0 of 1"):
        dbutil.run(benchmark.ensure_prepared(dataset))
    with pytest.raises(BenchmarkCorpusError, match="fixture-corpus"):
        dbutil.run(benchmark.prepare(dataset))  # a short prepare fails the exact-count check


def test_run_purges_the_isolated_corpus_unless_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    purged: list[Scopes] = []

    async def fake_purge(scope: Scopes) -> None:
        purged.append(scope)

    async def prepared(self: BenchmarkRunner, dataset: BenchmarkDataset) -> None:
        return None

    async def answer(
        self: BenchmarkRunner, dataset: BenchmarkDataset, question: BenchmarkQuestion
    ) -> BenchmarkAnswer:
        return BenchmarkAnswer(answer="candidate")

    monkeypatch.setattr(runner_module, "purge_scope", fake_purge)
    monkeypatch.setattr(BenchmarkRunner, "ensure_prepared", prepared)
    monkeypatch.setattr(BenchmarkRunner, "answer", answer)
    dataset = sample_dataset(sampled_questions=True)

    dbutil.run(runner().run(dataset, prepare=False))  # keep defaults to False

    assert purged == [BenchmarkRunner.scope(dataset)]


def test_answer_binds_scope_authority_and_asker_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[User, int | None, int]] = []
    standings: list[Scopes] = []

    async def context(
        query: str,
        user: User,
        token_budget: int | None,
        k: int,
    ) -> tuple[Candidate, ...]:
        del query
        calls.append((user, token_budget, k))
        standings.append(user.scopes.read)
        return (Candidate(lane="facts", line="User_1 selected the current plan."),)

    monkeypatch.setattr(runner_module, "recall", context)
    dataset = sample_dataset()
    benchmark = runner()
    answer = dbutil.run(benchmark.answer(dataset, dataset.questions[0]))

    assert answer == BenchmarkAnswer(answer="The current plan.")
    assert calls[0][1:] == (None, 10)
    assert calls[0][0].id not in benchmark.scope(dataset)
    assert calls[0][0].label == "User_1"
    assert standings == [benchmark.scope(dataset)]


@dataclass
class KindEvaluator(Evaluator[BenchmarkQuestion, BenchmarkAnswer, None]):
    def evaluate(
        self, ctx: EvaluatorContext[BenchmarkQuestion, BenchmarkAnswer, None]
    ) -> dict[str, bool]:
        return {"correct": ctx.inputs.kind is QuestionKind.user_implicit}


def test_pydantic_evals_aggregates_families_and_labels_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def prepared(self: BenchmarkRunner, dataset: BenchmarkDataset) -> None:
        return None

    async def answer(
        self: BenchmarkRunner,
        dataset: BenchmarkDataset,
        question: BenchmarkQuestion,
    ) -> BenchmarkAnswer:
        return BenchmarkAnswer(answer="candidate")

    monkeypatch.setattr(BenchmarkRunner, "ensure_prepared", prepared)
    monkeypatch.setattr(BenchmarkRunner, "answer", answer)
    dataset = sample_dataset(sampled_questions=True)
    report = dbutil.run(runner(KindEvaluator()).run(dataset, prepare=False, keep=True))

    assert report.accuracy == 0.5 and report.failed == 0
    assert report.by_kind == {
        QuestionKind.user_implicit: 1.0,
        QuestionKind.abstention: 0.0,
    }
    assert report.agent_model == "answer-test" and report.judge_model == "judge-test"
    assert not report.reference_protocol
    assert not report.publishable and "diagnostic" in report.render()


def test_reference_protocol_requires_the_released_models_and_retrieval_depth() -> None:
    model = TestModel(model_name="gpt-5")
    assert BenchmarkRunner(model, model, progress=False).reference_protocol
    assert not BenchmarkRunner(model, model, k=8, progress=False).reference_protocol


def test_configured_runner_uses_the_explicit_eval_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner_module,
        "provider_settings",
        lambda: SimpleNamespace(
            llm_url="http://extract/v1",
            llm_api_key="extract-key",
            llm_model="extract-model",
        ),
    )
    monkeypatch.setattr(runner_module.settings, "eval_url", "http://eval/v1")
    monkeypatch.setattr(runner_module.settings, "eval_api_key", "eval-key")
    monkeypatch.setattr(runner_module.settings, "eval_model", "answer-model")
    monkeypatch.setattr(runner_module.settings, "eval_judge_model", "judge-model")

    benchmark = BenchmarkRunner.configured(k=7)

    assert benchmark.k == 7
    assert benchmark.agent_model.model_name == "answer-model"
    assert benchmark.judge_model.model_name == "judge-model"
    assert benchmark.agent_model.profile["default_structured_output_mode"] == "native"
    assert benchmark.judge_model.profile["supports_json_schema_output"]


def test_corpus_state_queries_the_exact_prepared_scope(migrated_db: None) -> None:
    async def read() -> BenchmarkCorpusState:
        await dbutil.reset_db()
        return await runner().corpus_state(sample_dataset())

    assert dbutil.run(read()) == BenchmarkCorpusState(documents=0, pending_chunks=0)


def test_ensure_prepared_accepts_an_exact_complete_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ready(self: BenchmarkRunner, dataset: BenchmarkDataset) -> BenchmarkCorpusState:
        return BenchmarkCorpusState(documents=len(dataset.messages), pending_chunks=0)

    monkeypatch.setattr(BenchmarkRunner, "corpus_state", ready)
    dbutil.run(runner().ensure_prepared(sample_dataset()))


def test_pydantic_evals_keeps_operational_failures_out_of_wrong_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def prepared(self: BenchmarkRunner, dataset: BenchmarkDataset) -> None:
        return None

    async def fail(
        self: BenchmarkRunner,
        dataset: BenchmarkDataset,
        question: BenchmarkQuestion,
    ) -> BenchmarkAnswer:
        raise RuntimeError(f"answer failed for {question.id}")

    monkeypatch.setattr(BenchmarkRunner, "prepare", prepared)
    monkeypatch.setattr(BenchmarkRunner, "answer", fail)

    report = dbutil.run(runner(KindEvaluator()).run(sample_dataset(), keep=True))

    assert report.failed == report.total == 2
    assert report.correct == 0 and not report.publishable
    assert all(result.error and "answer failed" in result.error for result in report.results)
    assert "q1 error=RuntimeError" in report.render()


@given(
    presence=st.lists(st.booleans(), min_size=1, max_size=20),
    absence=st.lists(st.booleans(), max_size=20),
)
def test_fama_matches_the_paper_equation_and_stays_bounded(
    presence: list[bool], absence: list[bool]
) -> None:
    result = FAMAScore.from_judgments(presence, absence)
    mpa = sum(presence) / len(presence)
    faa = sum(absence) / len(absence) if absence else 1.0
    weight = len(absence) / (len(presence) + len(absence))
    assert result.score == max(0.0, mpa - weight * (1.0 - faa))
    assert 0.0 <= result.score <= 1.0


def test_fama_rejects_an_item_without_current_memory_criteria() -> None:
    with pytest.raises(ValueError, match="memory-presence"):
        FAMAScore.from_judgments([])
