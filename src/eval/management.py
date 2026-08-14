import asyncio
from collections.abc import Sequence
from math import ceil
from time import perf_counter
from typing import ClassVar, Literal, TypeIs

from patos import FrozenModel
from pydantic import NonNegativeFloat, NonNegativeInt, PositiveInt
from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlmodel import select

from aizk.config import settings as aizk_settings
from aizk.retrieval import recall
from aizk.store import Document
from aizk.store.identity import User

from .config import settings


def _is_management_kind(value: str | None) -> TypeIs[Literal["area", "project"]]:
    """Narrow a stored subject type to the two management kinds this report accepts."""
    return value == "area" or value == "project"


class ManagementQuestions(FrozenModel):
    """Twenty grounded retrieval probes for one Area or Project brief."""

    area_templates: ClassVar[tuple[str, ...]] = (
        "What is the vision for the {name} Area?",
        "What standard of care defines the {name} Area?",
        "What is the current state of the {name} Area?",
        "Which Projects are active in the {name} Area?",
        "What changed most recently in the {name} Area?",
        "What are the main problems in the {name} Area?",
        "What risks need attention in the {name} Area?",
        "Which decisions remain unresolved in the {name} Area?",
        "What are the next actions for the {name} Area?",
        "Which routines sustain the {name} Area?",
        "Which measures show whether the {name} Area is healthy?",
        "Which repositories and resources belong to the {name} Area?",
        "How does the {name} Area relate to other Areas?",
        "Which current commitments belong to the {name} Area?",
        "Which assumptions about the {name} Area may now be stale?",
        "What work was recently completed in the {name} Area?",
        "What part of the {name} Area is being neglected?",
        "What should not be treated as active work in the {name} Area?",
        "What event or evidence should change the current plan for the {name} Area?",
        "Summarize the vision, current state, problems, and next actions for {name}.",
    )
    project_templates: ClassVar[tuple[str, ...]] = (
        "Why does the {name} Project exist?",
        "Which Area owns the {name} Project?",
        "What is the vision for the {name} Project?",
        "What concrete goal defines the {name} Project?",
        "What is the status of the {name} Project?",
        "What phase is the {name} Project in now?",
        "What is the latest verified result from the {name} Project?",
        "What is the next milestone for the {name} Project?",
        "What is the next action for the {name} Project?",
        "What currently blocks the {name} Project?",
        "Which decision is still open in the {name} Project?",
        "What is the main risk to the {name} Project?",
        "What deadline governs the {name} Project?",
        "Who owns or collaborates on the {name} Project?",
        "Which repository and resources support the {name} Project?",
        "Which dependencies constrain the {name} Project?",
        "Which other Projects relate to the {name} Project?",
        "Which older plans for the {name} Project were superseded?",
        "What is the success condition for the {name} Project?",
        "What would close or reopen the {name} Project?",
    )

    name: str
    kind: Literal["area", "project"]

    @property
    def questions(self) -> tuple[str, ...]:
        """Render the subject name into the contract's twenty probes."""
        templates = self.area_templates if self.kind == "area" else self.project_templates
        return tuple(template.format(name=self.name) for template in templates)


class ManagementSubject(FrozenModel):
    """One managed source whose own brief is the retrieval reference."""

    name: str
    kind: Literal["area", "project"]
    status: str | None = None


class ManagementProbe(FrozenModel):
    """One contract question's reference rank and end-to-end latency."""

    rank: PositiveInt | None
    latency_ms: NonNegativeFloat


class ManagementResult(FrozenModel):
    """The retrieval result of every contract question for one subject."""

    subject: ManagementSubject
    probes: tuple[ManagementProbe, ...]

    @property
    def hits(self) -> int:
        """How many questions retrieved the subject's own current brief."""
        return sum(probe.rank is not None for probe in self.probes)

    @property
    def firsts(self) -> int:
        """How many questions ranked the subject's own current brief first."""
        return sum(probe.rank == 1 for probe in self.probes)

    @property
    def reciprocal_rank(self) -> float:
        """Mean reciprocal source rank over all twenty questions."""
        return sum(1.0 / probe.rank for probe in self.probes if probe.rank is not None) / len(
            self.probes
        )


class ManagementReport(FrozenModel):
    """Area and Project retrieval quality under the shared management contract."""

    results: tuple[ManagementResult, ...]

    @property
    def questions(self) -> NonNegativeInt:
        """Total contract questions evaluated."""
        return sum(len(result.probes) for result in self.results)

    @property
    def hits(self) -> NonNegativeInt:
        """Total questions that retrieved their reference brief."""
        return sum(result.hits for result in self.results)

    @property
    def firsts(self) -> NonNegativeInt:
        """Total questions that ranked their reference brief first."""
        return sum(result.firsts for result in self.results)

    def latency(self, percentile: float) -> float:
        """Return a nearest-rank end-to-end latency percentile in milliseconds."""
        samples = sorted(probe.latency_ms for result in self.results for probe in result.probes)
        return samples[max(0, ceil(percentile * len(samples)) - 1)] if samples else 0.0

    def render(self) -> str:
        """Render subject inclusion, first rank, MRR, and latency as a compact table."""
        lines = ["kind     status     hit    first      mrr   p50 ms  subject"]
        for result in self.results:
            status = result.subject.status or "-"
            lines.append(
                f"{result.subject.kind:8} {status:10} {result.hits:2}/{len(result.probes):2}  "
                f"{result.firsts:2}/{len(result.probes):2}  {result.reciprocal_rank:.3f}  "
                f"{ManagementReport(results=(result,)).latency(0.5):7.1f}  "
                f"{result.subject.name}"
            )
        hit_rate = self.hits / self.questions if self.questions else 0.0
        first_rate = self.firsts / self.questions if self.questions else 0.0
        lines.append(
            f"overall  {self.hits}/{self.questions} hit {hit_rate:.3f}  "
            f"{self.firsts}/{self.questions} first {first_rate:.3f}  "
            f"p50 {self.latency(0.5):.1f} ms  p95 {self.latency(0.95):.1f} ms"
        )
        return "\n".join(lines)


class ManagementBenchmark:
    """Evaluate every visible managed brief against its twenty grounded probes."""

    __slots__ = ("budget", "concurrency", "k", "user")

    def __init__(
        self,
        user: User,
        k: int = 8,
        budget: int = aizk_settings.context_token_budget,
        concurrency: int = settings.concurrency,
    ) -> None:
        self.user = user
        self.k = k
        self.budget = budget
        self.concurrency = concurrency

    async def subjects(self, kinds: Sequence[str]) -> tuple[ManagementSubject, ...]:
        """Read visible Area and Project identities from declared source documents."""
        requested = tuple(
            kind.casefold() for kind in kinds if kind.casefold() in {"area", "project"}
        )
        if not requested:
            return ()
        async with self.user as session:
            rows = await session.exec(
                select(Document.title, Document.subject_type)
                .where(
                    Document.subject_type.in_(requested),
                    Document.title.is_not(None),
                    or_(
                        Document.expires_at.is_(None),
                        Document.expires_at > func.now(),
                    ),
                )
                .ext(distinct_on(Document.subject_type, Document.title))
                .order_by(Document.subject_type, Document.title, Document.updated_at.desc())
            )
        return tuple(
            ManagementSubject(name=name, kind=kind)
            for name, kind in rows
            if name is not None and _is_management_kind(kind)
        )

    def caller(self) -> User:
        """Create an independent transaction stack for one concurrent recall."""
        return User.authorized(
            self.user.id,
            read=self.user.scopes.read,
            write=self.user.scopes.write,
            public=self.user.scopes.public,
            label=self.user.label,
            organizations=self.user.organizations,
        )

    async def probe(self, question: str, subject: str) -> ManagementProbe:
        """Measure the packed rank of the subject's own brief for one question."""
        started = perf_counter()
        candidates = await recall(
            question,
            self.caller(),
            k=self.k,
            token_budget=self.budget,
        )
        rank = next(
            (
                rank
                for rank, candidate in enumerate(candidates, 1)
                if candidate.source_title == subject
            ),
            None,
        )
        return ManagementProbe(rank=rank, latency_ms=(perf_counter() - started) * 1_000)

    async def run(self, kinds: Sequence[str] = ("area", "project")) -> ManagementReport:
        """Evaluate twenty questions per visible subject with bounded concurrency."""
        semaphore = asyncio.Semaphore(self.concurrency)

        async def probe(question: str, subject: str) -> ManagementProbe:
            async with semaphore:
                return await self.probe(question, subject)

        results = []
        for subject in await self.subjects(kinds):
            questions = ManagementQuestions(name=subject.name, kind=subject.kind).questions
            probes = await asyncio.gather(
                *(probe(question, subject.name) for question in questions)
            )
            results.append(ManagementResult(subject=subject, probes=tuple(probes)))
        return ManagementReport(results=tuple(results))
