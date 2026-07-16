import html
from datetime import datetime
from enum import StrEnum, auto

from patos import FrozenModel
from pydantic import Field, field_validator


class GeneratedQuestion(FrozenModel):
    """A question synthesized from one fact for an internal retrieval probe."""

    question: str = Field(
        max_length=256,
        description="one natural question whose answer is the source fact",
    )


class QuestionKind(StrEnum):
    """The six orthogonal GroupMemBench question families."""

    multi_hop = auto()
    knowledge_update = auto()
    temporal = auto()
    user_implicit = auto()
    term_ambiguity = auto()
    abstention = auto()


class BenchmarkMessage(FrozenModel):
    """One authored group message with the structure team memory must retain."""

    id: str
    content: str
    author: str
    role: str | None = None
    timestamp: datetime
    channel: str = ""
    reply_to: str | None = None
    phase: str | None = None
    topic: str | None = None
    noise: bool = False
    decision: bool = False

    @field_validator("content")
    @classmethod
    def clean_content(cls, content: str) -> str:
        """Decode HTML entities and remove transport whitespace from message text."""
        return html.unescape(content).strip()


class BenchmarkQuestion(FrozenModel):
    """One perspective-bound question and its expected answer."""

    id: str
    question: str
    answer: str
    asking_user: str
    kind: QuestionKind

    @property
    def should_abstain(self) -> bool:
        """Whether correctness requires refusing because no answer exists."""
        return self.kind is QuestionKind.abstention


class BenchmarkDataset(FrozenModel):
    """One isolated external conversation corpus and its evaluation questions."""

    name: str
    domain: str
    fingerprint: str
    messages: tuple[BenchmarkMessage, ...]
    questions: tuple[BenchmarkQuestion, ...]
    complete_corpus: bool = True
    sampled_questions: bool = False


class BenchmarkCorpusState(FrozenModel):
    """Prepared document count and graph work still pending for one corpus fingerprint."""

    documents: int
    pending_chunks: int

    def ready(self, expected_documents: int) -> bool:
        """Whether every expected document exists and every chunk finished graph extraction."""
        return self.documents == expected_documents and self.pending_chunks == 0


class BenchmarkAnswer(FrozenModel):
    """One answer generated strictly from recalled benchmark context."""

    answer: str
    abstained: bool = False


class BenchmarkResult(FrozenModel):
    """One benchmark answer with its judge verdict and operational evidence."""

    question_id: str
    kind: QuestionKind
    asking_user: str
    expected: str
    answer: str
    abstained: bool
    correct: bool
    reason: str | None = None
    error: str | None = None
    duration_seconds: float | None = None


class BenchmarkReport(FrozenModel):
    """Group-memory accuracy with per-family scores and run validity metadata."""

    benchmark: str
    domain: str
    fingerprint: str
    agent_model: str
    judge_model: str
    total: int
    correct: int
    failed: int
    accuracy: float
    by_kind: dict[QuestionKind, float]
    complete_corpus: bool
    sampled_questions: bool
    solvability_filtered: bool
    reference_protocol: bool
    publishable: bool
    results: tuple[BenchmarkResult, ...]

    def render(self) -> str:
        """Render a compact scorecard that cannot hide partial or failed runs."""
        kinds = " ".join(f"{kind.value}={score:.3f}" for kind, score in self.by_kind.items())
        status = "publishable" if self.publishable else "diagnostic"
        summary = (
            f"{self.benchmark} {self.domain} {status} n={self.total} "
            f"accuracy={self.accuracy:.3f} failed={self.failed}\n{kinds}"
        )
        failures = "\n".join(
            f"{result.question_id} error={result.error}" for result in self.results if result.error
        )
        return f"{summary}\n{failures}" if failures else summary
