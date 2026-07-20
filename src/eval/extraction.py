import asyncio
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from time import perf_counter

from openai import LengthFinishReasonError
from patos import FrozenModel
from pydantic import ValidationError
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from aizk.extract.extractor import Extractor
from aizk.extract.models import TimedFact
from aizk.graph.grounding import GroundedProjection, ProjectionQuality
from aizk.provenance import EpistemicKind

from .metrics import percentile, ratio


def normalized(text: str) -> str:
    """Normalize one benchmark label for exact, case-insensitive comparison."""
    return " ".join(text.casefold().split())


class ExtractionTarget(FrozenModel):
    """One acceptable graph fact and its optional semantic metadata."""

    subjects: frozenset[str]
    predicate: str
    objects: frozenset[str]
    kind: EpistemicKind | None = None
    valid_from: date | None = None

    def matches(self, fact: TimedFact) -> bool:
        """Whether a proposed fact expresses this target triple."""
        return (
            normalized(fact.subject) in {normalized(name) for name in self.subjects}
            and normalized(fact.predicate) == normalized(self.predicate)
            and normalized(fact.object_) in {normalized(name) for name in self.objects}
        )

    def metadata(self, fact: TimedFact) -> tuple[int, int]:
        """Return checked and correct semantic fields for one matching fact."""
        checked = correct = 0
        if self.kind is not None:
            checked += 1
            correct += fact.kind is self.kind
        if self.valid_from is not None:
            checked += 1
            correct += fact.valid_from is not None and fact.valid_from.date() == self.valid_from
        return checked, correct


class ExtractionCase(FrozenModel):
    """One source span with human-verified graph targets."""

    id: str
    text: str
    targets: tuple[ExtractionTarget, ...]


class ExtractionCaseResult(FrozenModel):
    """One model extraction scored before and after deterministic grounding."""

    id: str
    duration_ms: float
    targets: int
    proposed_matches: int
    accepted_matches: int
    metadata_checked: int
    metadata_correct: int
    quality: ProjectionQuality
    accepted: tuple[TimedFact, ...]
    error: str | None = None


class ExtractionReport(FrozenModel):
    """Aggregate graph extraction fidelity, latency, and inspectable case results."""

    model: str
    cases: int
    failed: int
    targets: int
    proposed_facts: int
    accepted_facts: int
    proposed_recall: float
    accepted_precision: float
    accepted_recall: float
    accepted_f1: float
    grounding_rate: float
    metadata_accuracy: float
    p50_ms: float
    p95_ms: float
    wall_ms: float
    concurrency: int
    cases_per_hour: float
    backlog: int
    backlog_hours: float
    results: tuple[ExtractionCaseResult, ...]

    @classmethod
    def score(
        cls,
        model: str,
        results: Sequence[ExtractionCaseResult],
        *,
        wall_ms: float | None = None,
        concurrency: int = 1,
        backlog: int = 10_704,
    ) -> ExtractionReport:
        """Aggregate case results without hiding failed model turns."""
        targets = sum(result.targets for result in results)
        proposed = sum(result.quality.proposed_facts for result in results)
        accepted = sum(result.quality.accepted_facts for result in results)
        proposed_matches = sum(result.proposed_matches for result in results)
        accepted_matches = sum(result.accepted_matches for result in results)
        metadata_checked = sum(result.metadata_checked for result in results)
        precision = ratio(accepted_matches, accepted)
        recall = ratio(accepted_matches, targets)
        measured_wall_ms = (
            sum(result.duration_ms for result in results) if wall_ms is None else wall_ms
        )
        successful = sum(result.error is None for result in results)
        cases_per_hour = ratio(successful * 3_600_000.0, measured_wall_ms)
        return cls(
            model=model,
            cases=len(results),
            failed=sum(result.error is not None for result in results),
            targets=targets,
            proposed_facts=proposed,
            accepted_facts=accepted,
            proposed_recall=ratio(proposed_matches, targets),
            accepted_precision=precision,
            accepted_recall=recall,
            accepted_f1=ratio(2.0 * precision * recall, precision + recall),
            grounding_rate=ratio(accepted, proposed),
            metadata_accuracy=ratio(
                sum(result.metadata_correct for result in results), metadata_checked, 1.0
            ),
            p50_ms=percentile([result.duration_ms for result in results], 50),
            p95_ms=percentile([result.duration_ms for result in results], 95),
            wall_ms=measured_wall_ms,
            concurrency=concurrency,
            cases_per_hour=cases_per_hour,
            backlog=backlog,
            backlog_hours=ratio(backlog, cases_per_hour),
            results=tuple(results),
        )

    def render(self) -> str:
        """Render the deployment scorecard and failed case names."""
        summary = (
            f"{self.model} extraction n={self.cases} failed={self.failed} "
            f"f1={self.accepted_f1:.3f} precision={self.accepted_precision:.3f} "
            f"recall={self.accepted_recall:.3f} proposal_recall={self.proposed_recall:.3f} "
            f"grounded={self.grounding_rate:.3f} metadata={self.metadata_accuracy:.3f} "
            f"p50={self.p50_ms:.1f}ms p95={self.p95_ms:.1f}ms "
            f"wall={self.wall_ms:.1f}ms concurrency={self.concurrency} "
            f"rate={self.cases_per_hour:.1f}/h backlog_eta={self.backlog_hours:.2f}h"
        )
        failures = "\n".join(
            f"{result.id} error={result.error}" for result in self.results if result.error
        )
        return f"{summary}\n{failures}" if failures else summary


class ExtractionBenchmark:
    """Run one extractor against source-grounded graph targets."""

    __slots__ = ("extractor",)

    def __init__(self, extractor: Extractor) -> None:
        self.extractor = extractor

    async def case(self, case: ExtractionCase) -> ExtractionCaseResult:
        """Extract and score one case, retaining bounded structured-output failures."""
        started = perf_counter()
        try:
            extraction = await self.extractor.extract(case.text)
        except (
            LengthFinishReasonError,
            ModelHTTPError,
            UnexpectedModelBehavior,
            ValidationError,
            ValueError,
        ) as error:
            return self.failed(case, started, error)
        grounded = GroundedProjection.from_extraction(extraction, case.text)
        proposed_matches, _, _ = self.matches(case.targets, extraction.facts)
        accepted_matches, metadata_checked, metadata_correct = self.matches(
            case.targets, grounded.facts
        )
        return ExtractionCaseResult(
            id=case.id,
            duration_ms=(perf_counter() - started) * 1000.0,
            targets=len(case.targets),
            proposed_matches=proposed_matches,
            accepted_matches=accepted_matches,
            metadata_checked=metadata_checked,
            metadata_correct=metadata_correct,
            quality=grounded.quality,
            accepted=tuple(grounded.facts),
        )

    @staticmethod
    def failed(
        case: ExtractionCase,
        started: float,
        error: (
            LengthFinishReasonError
            | ModelHTTPError
            | UnexpectedModelBehavior
            | ValidationError
            | ValueError
        ),
    ) -> ExtractionCaseResult:
        """Represent one bounded model output failure without inventing predictions."""
        return ExtractionCaseResult(
            id=case.id,
            duration_ms=(perf_counter() - started) * 1000.0,
            targets=len(case.targets),
            proposed_matches=0,
            accepted_matches=0,
            metadata_checked=0,
            metadata_correct=0,
            quality=ProjectionQuality(
                proposed_entities=0,
                accepted_entities=0,
                proposed_facts=0,
                accepted_facts=0,
            ),
            accepted=(),
            error=f"{type(error).__name__}: {error}",
        )

    @staticmethod
    def matches(
        targets: Sequence[ExtractionTarget], facts: Sequence[TimedFact]
    ) -> tuple[int, int, int]:
        """Count one-to-one target hits and semantic metadata."""
        matched = checked = correct = 0
        remaining = list(facts)
        for target in targets:
            match = next(
                ((index, fact) for index, fact in enumerate(remaining) if target.matches(fact)),
                None,
            )
            if match is None:
                continue
            index, fact = match
            remaining.pop(index)
            matched += 1
            target_checked, target_correct = target.metadata(fact)
            checked += target_checked
            correct += target_correct
        return matched, checked, correct

    async def run(self, cases: Sequence[ExtractionCase], model: str) -> ExtractionReport:
        """Run cases sequentially so latency and memory remain comparable across models."""
        return await self.run_concurrent(cases, model)

    async def run_concurrent(
        self,
        cases: Sequence[ExtractionCase],
        model: str,
        concurrency: int = 1,
        backlog: int = 10_704,
    ) -> ExtractionReport:
        """Measure bounded parallel extraction and project the configured backlog."""
        if concurrency < 1:
            raise ValueError("extraction benchmark concurrency must be positive")
        limiter = asyncio.Semaphore(concurrency)
        started = perf_counter()
        results = await asyncio.gather(*(self._limited_case(case, limiter) for case in cases))
        return ExtractionReport.score(
            model,
            results,
            wall_ms=(perf_counter() - started) * 1000.0,
            concurrency=concurrency,
            backlog=backlog,
        )

    async def _limited_case(
        self,
        case: ExtractionCase,
        limiter: asyncio.Semaphore,
    ) -> ExtractionCaseResult:
        """Run one case inside the benchmark-wide concurrency bound."""
        async with limiter:
            return await self.case(case)


def load_extraction_cases(path: Path) -> tuple[ExtractionCase, ...]:
    """Load nonblank JSONL benchmark cases from disk."""
    return tuple(
        ExtractionCase.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
