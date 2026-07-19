from collections import Counter
from functools import partial
from urllib.parse import quote

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, LLMJudge
from pydantic_evals.reporting import ReportCase, ReportCaseFailure
from sqlmodel import select

from aizk.config import settings as aizk_settings
from aizk.extract.ingest import TextSource, ingest_texts
from aizk.graph.build import GraphClients, build_graph
from aizk.provenance import CaptureContext
from aizk.retrieval import RecallResult, recall
from aizk.serving.base import llm_model
from aizk.store import Chunk, Document
from aizk.store.identity import User
from aizk.types import Scopes

from .cleanup import purge_scope
from .config import settings
from .models import (
    BenchmarkAnswer,
    BenchmarkCorpusState,
    BenchmarkDataset,
    BenchmarkMessage,
    BenchmarkQuestion,
    BenchmarkReport,
    BenchmarkResult,
)

_ANSWER_SYSTEM = (
    "Answer using only the recalled team memory. Resolve first-person language from the named\n"
    "asking user. If the memory does not contain the answer, abstain without guessing."
)

_CORRECTNESS_RUBRIC = (
    "Pass only when the candidate answer is semantically equivalent to the expected answer. "
    "For an expected abstention, the candidate must abstain and must not invent an answer. "
    "Treat missing, vague, contradictory, and partially correct answers as failures."
)


class BenchmarkCorpusError(RuntimeError):
    """Prepared benchmark rows do not match the selected immutable corpus."""


class BenchmarkRunner:
    """Prepare, recall, answer, and judge one isolated conversation benchmark."""

    def __init__(
        self,
        agent_model: Model,
        judge_model: Model,
        k: int = 10,
        token_budget: int | None = None,
        concurrency: int = 4,
        progress: bool = True,
        evaluator: Evaluator[BenchmarkQuestion, BenchmarkAnswer, None] | None = None,
    ) -> None:
        self.agent_model = agent_model
        self.judge_model = judge_model
        self.k = k
        self.token_budget: int = (
            token_budget if token_budget is not None else aizk_settings.context_token_budget
        )
        self.concurrency = concurrency
        self.progress = progress
        self.agent = Agent[None, BenchmarkAnswer](
            agent_model,
            output_type=BenchmarkAnswer,
            deps_type=type(None),
            system_prompt=_ANSWER_SYSTEM,
            model_settings={"temperature": 0.0, "max_tokens": settings.max_tokens},
        )
        self.judge = evaluator or LLMJudge(
            rubric=_CORRECTNESS_RUBRIC,
            model=judge_model,
            include_input=True,
            include_expected_output=True,
            model_settings={"temperature": 0.0, "max_tokens": 256},
            assertion={"evaluation_name": "correct", "include_reason": True},
        )

    @classmethod
    def configured(cls, k: int = 10, token_budget: int | None = None) -> BenchmarkRunner:
        """Build the runner from the explicit eval endpoint or extraction fallback."""
        url = settings.url or aizk_settings.llm_url
        api_key = settings.api_key or aizk_settings.llm_api_key or "local"
        agent_name = settings.model or aizk_settings.llm_model
        judge_name = settings.judge_model or agent_name
        return cls(
            llm_model(url, api_key, agent_name, aizk_settings.llm_timeout),
            llm_model(url, api_key, judge_name, aizk_settings.llm_timeout),
            k=k,
            token_budget=token_budget,
            concurrency=settings.concurrency,
        )

    @property
    def reference_protocol(self) -> bool:
        """Whether retrieval and answer judging match the released comparison protocol."""
        return (
            self.k == 10
            and self.agent_model.model_name == "gpt-5"
            and self.judge_model.model_name == "gpt-5"
        )

    @staticmethod
    def scope(dataset: BenchmarkDataset) -> Scopes:
        """Return the deterministic scope for one exact benchmark corpus revision."""
        return frozenset(
            {
                aizk_settings.scope_id(
                    f"benchmark:{dataset.name}:{dataset.domain}:{dataset.fingerprint}"
                )
            }
        )

    @classmethod
    def source(cls, dataset: BenchmarkDataset, message: BenchmarkMessage) -> TextSource:
        """Map one group message onto an authored and structured Aizk source."""
        capture = CaptureContext(
            speaker_label=message.author,
            speaker_role=message.role,
            channel=message.channel,
            reply_to=message.reply_to,
            phase=message.phase,
            topic=message.topic,
            observed_at=message.timestamp,
        )
        return TextSource(
            text=message.content,
            title=f"{message.channel} {message.id}",
            source_uri=(
                f"groupmembench://{dataset.fingerprint}/{quote(dataset.domain, safe='')}/"
                f"{quote(message.channel, safe='')}/{quote(message.id, safe='')}"
            ),
            created_by=aizk_settings.subject_id(f"benchmark:{dataset.name}:{message.author}"),
            scopes=cls.scope(dataset),
            capture=capture,
        )

    async def corpus_state(self, dataset: BenchmarkDataset) -> BenchmarkCorpusState:
        """Read document completeness and pending graph work in one database round trip."""
        scope = self.scope(dataset)
        prefix = f"groupmembench://{dataset.fingerprint}/%"
        matching = (
            Document.scopes == sorted(scope),
            Document.source_uri.like(prefix),
        )
        documents = select(Document.id.count()).where(*matching).scalar_subquery()
        pending = (
            select(Chunk.id.count())
            .join(Document, Document.id == Chunk.document_id)
            .where(*matching, Chunk.processed_at.is_(None))
            .scalar_subquery()
        )
        async with User.system(scope) as session:
            result = await session.exec(select(documents, pending))
            document_count, pending_count = result.one()
        return BenchmarkCorpusState(
            documents=int(document_count), pending_chunks=int(pending_count)
        )

    async def prepare(self, dataset: BenchmarkDataset) -> int:
        """Idempotently import and build the exact fingerprint, then verify its row count."""
        scope = self.scope(dataset)
        await ingest_texts(
            User.system(scope), [self.source(dataset, message) for message in dataset.messages]
        )
        await build_graph(GraphClients.from_settings(aizk_settings), scopes=scope)
        state = await self.corpus_state(dataset)
        if not state.ready(len(dataset.messages)):
            raise BenchmarkCorpusError(
                f"prepared {state.documents} of {len(dataset.messages)} messages with "
                f"{state.pending_chunks} pending chunks for {dataset.fingerprint}"
            )
        return state.documents

    async def ensure_prepared(self, dataset: BenchmarkDataset) -> None:
        """Fail before scoring when reuse points at an absent or incomplete corpus."""
        state = await self.corpus_state(dataset)
        if not state.ready(len(dataset.messages)):
            raise BenchmarkCorpusError(
                f"prepared corpus has {state.documents} of {len(dataset.messages)} messages "
                f"and {state.pending_chunks} pending chunks"
            )

    async def answer(
        self, dataset: BenchmarkDataset, question: BenchmarkQuestion
    ) -> BenchmarkAnswer:
        """Recall with benchmark authority and answer only from the recalled evidence."""
        scope = self.scope(dataset)
        asker = aizk_settings.subject_id(f"benchmark:{dataset.name}:{question.asking_user}")
        user = User.authorized(
            asker,
            read=scope,
            write=scope,
            label=question.asking_user,
        )
        candidates = await recall(
            question.question,
            user=user,
            token_budget=self.token_budget,
            k=self.k,
        )
        context = await RecallResult.from_candidates(candidates).to_markdown()
        prompt = (
            f"Asking user\n{question.asking_user}\n\nQuestion\n{question.question}\n\n"
            f"Recalled memory\n{context}"
        )
        return (await self.agent.run(prompt)).output

    async def run(
        self, dataset: BenchmarkDataset, prepare: bool = True, keep: bool = False
    ) -> BenchmarkReport:
        """Evaluate an isolated corpus and remove it unless retention is requested."""
        try:
            return await self.evaluate(dataset, prepare)
        finally:
            if not keep:
                await purge_scope(self.scope(dataset))

    async def evaluate(self, dataset: BenchmarkDataset, prepare: bool = True) -> BenchmarkReport:
        """Execute typed cases and retain wrong answers separately from failures."""
        if prepare:
            await self.prepare(dataset)
        else:
            await self.ensure_prepared(dataset)
        cases = self._cases(dataset)
        evaluated = await cases.evaluate(
            partial(self.answer, dataset),
            name=f"aizk-{dataset.domain}",
            max_concurrency=self.concurrency,
            progress=self.progress,
        )
        results = {case.inputs.id: self._result(case) for case in evaluated.cases}
        results.update(
            {failure.inputs.id: self._failure(failure) for failure in evaluated.failures}
        )
        return self._report(dataset, tuple(results[question.id] for question in dataset.questions))

    def _cases(
        self, dataset: BenchmarkDataset
    ) -> Dataset[BenchmarkQuestion, BenchmarkAnswer, None]:
        return Dataset(
            name=f"{dataset.name}-{dataset.domain}-{dataset.fingerprint}",
            cases=[
                Case(
                    name=question.id,
                    inputs=question,
                    expected_output=BenchmarkAnswer(
                        answer=question.answer,
                        abstained=question.should_abstain,
                    ),
                )
                for question in dataset.questions
            ],
            evaluators=[self.judge],
        )

    @staticmethod
    def _result(
        case: ReportCase[BenchmarkQuestion, BenchmarkAnswer, None],
    ) -> BenchmarkResult:
        verdict = case.assertions.get("correct")
        return BenchmarkResult(
            question_id=case.inputs.id,
            kind=case.inputs.kind,
            asking_user=case.inputs.asking_user,
            expected=case.expected_output.answer if case.expected_output else "",
            answer=case.output.answer,
            abstained=case.output.abstained,
            correct=verdict.value if verdict else False,
            reason=verdict.reason if verdict else None,
            error="; ".join(failure.error_message for failure in case.evaluator_failures) or None,
            duration_seconds=case.total_duration,
        )

    @staticmethod
    def _failure(
        failure: ReportCaseFailure[BenchmarkQuestion, BenchmarkAnswer, None],
    ) -> BenchmarkResult:
        return BenchmarkResult(
            question_id=failure.inputs.id,
            kind=failure.inputs.kind,
            asking_user=failure.inputs.asking_user,
            expected=failure.expected_output.answer if failure.expected_output else "",
            answer="",
            abstained=False,
            correct=False,
            error=failure.error_message,
        )

    def _report(
        self, dataset: BenchmarkDataset, ordered: tuple[BenchmarkResult, ...]
    ) -> BenchmarkReport:
        totals = Counter(result.kind for result in ordered)
        correct = Counter(result.kind for result in ordered if result.correct)
        failed = sum(result.error is not None for result in ordered)
        solvability_filtered = dataset.domain in {"Finance", "Technology"}
        correct_total = sum(result.correct for result in ordered)
        return BenchmarkReport(
            benchmark=dataset.name,
            domain=dataset.domain,
            fingerprint=dataset.fingerprint,
            agent_model=self.agent_model.model_name,
            judge_model=self.judge_model.model_name,
            total=len(ordered),
            correct=correct_total,
            failed=failed,
            accuracy=correct_total / max(len(ordered), 1),
            by_kind={kind: correct[kind] / count for kind, count in totals.items()},
            complete_corpus=dataset.complete_corpus,
            sampled_questions=dataset.sampled_questions,
            solvability_filtered=solvability_filtered,
            reference_protocol=self.reference_protocol,
            publishable=all(
                (
                    dataset.complete_corpus,
                    not dataset.sampled_questions,
                    solvability_filtered,
                    self.reference_protocol,
                    failed == 0,
                )
            ),
            results=ordered,
        )
