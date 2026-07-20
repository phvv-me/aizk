from datetime import UTC, date, datetime
from pathlib import Path

import dbutil
import pytest
from pydantic import ConfigDict
from pydantic_ai.exceptions import UnexpectedModelBehavior

from aizk.extract.extractor import Extractor
from aizk.extract.models import ExtractedEntity, Extraction, TimedFact
from aizk.provenance import EpistemicKind
from eval.extraction import (
    ExtractionBenchmark,
    ExtractionCase,
    ExtractionReport,
    ExtractionTarget,
    load_extraction_cases,
    normalized,
)


class StaticExtractor(Extractor):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    extraction: Extraction | UnexpectedModelBehavior | ValueError

    async def extract(self, text: str) -> Extraction:
        assert text
        if isinstance(self.extraction, UnexpectedModelBehavior | ValueError):
            raise self.extraction
        return self.extraction


def target(
    predicate: str = "uses",
    objects: frozenset[str] = frozenset({"PostgreSQL", "Postgres"}),
    kind: EpistemicKind = EpistemicKind.experience,
    valid_from: date = date(2026, 7, 2),
) -> ExtractionTarget:
    return ExtractionTarget(
        subjects=frozenset({"Aizk"}),
        predicate=predicate,
        objects=objects,
        kind=kind,
        valid_from=valid_from,
    )


def test_extraction_benchmark_scores_grounding_aliases_and_semantic_metadata() -> None:
    text = "On July 2, 2026, Aizk uses PostgreSQL."
    supported = TimedFact(
        subject="aizk",
        predicate="USES",
        object="Postgres",
        statement="Aizk uses PostgreSQL.",
        quote="Aizk uses PostgreSQL",
        valid_from=datetime(2026, 7, 2, tzinfo=UTC),
        kind=EpistemicKind.experience,
    )
    unsupported = TimedFact(
        subject="Aizk",
        predicate="related_to",
        object="PostgreSQL",
        statement="Aizk is related to PostgreSQL.",
    )
    case = ExtractionCase(
        id="grounded",
        text=text,
        targets=(target(), target(predicate="depends_on", objects=frozenset({"Python"}))),
    )
    extraction = Extraction(
        entities=[
            ExtractedEntity(name="Aizk", type="Project"),
            ExtractedEntity(name="Postgres", type="Tool"),
        ],
        facts=[supported, unsupported],
    )

    report = dbutil.run(
        ExtractionBenchmark(StaticExtractor(extraction=extraction)).run([case], "model-a")
    )

    assert normalized("  AIZK\nMemory ") == "aizk memory"
    assert report.model == "model-a"
    assert report.targets == 2
    assert report.proposed_facts == 2
    assert report.accepted_facts == 1
    assert report.proposed_recall == 0.5
    assert report.accepted_precision == 1.0
    assert report.accepted_recall == 0.5
    assert report.accepted_f1 == 2 / 3
    assert report.grounding_rate == 0.5
    assert report.metadata_accuracy == 1.0
    assert report.results[0].accepted[0].subject == "Aizk"
    assert report.results[0].accepted[0].object_ == "Postgres"
    assert report.results[0].quality.missing_quote == 1
    assert report.concurrency == 1
    assert report.cases_per_hour > 0.0
    assert report.backlog == 10_704
    assert report.backlog_hours > 0.0
    assert "f1=0.667" in report.render()
    assert "backlog_eta=" in report.render()


def test_extraction_target_reports_wrong_metadata_and_missing_matches() -> None:
    fact = TimedFact(
        subject="Aizk",
        predicate="uses",
        object="PostgreSQL",
        statement="Aizk uses PostgreSQL.",
        quote="Aizk uses PostgreSQL",
        valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        kind=EpistemicKind.world,
    )
    expected = target()

    assert expected.matches(fact)
    assert expected.metadata(fact) == (2, 0)
    assert expected.model_copy(update={"valid_from": None}).metadata(fact) == (1, 0)
    assert expected.model_copy(update={"kind": None}).metadata(fact) == (1, 0)
    assert ExtractionBenchmark.matches([expected], []) == (0, 0, 0)
    assert ExtractionBenchmark.matches([expected, expected], [fact]) == (1, 2, 0)


def test_extraction_reports_define_failure_and_empty_boundaries() -> None:
    case = ExtractionCase(id="bad-json", text="source", targets=(target(),))

    failed = dbutil.run(
        ExtractionBenchmark(StaticExtractor(extraction=ValueError("no parsed output"))).run(
            [case], "broken"
        )
    )

    assert failed.failed == 1
    assert failed.accepted_recall == 0.0
    assert failed.metadata_accuracy == 1.0
    assert failed.cases_per_hour == 0.0
    assert failed.backlog_hours == 0.0
    assert failed.results[0].error == "ValueError: no parsed output"
    assert "bad-json error=ValueError" in failed.render()

    empty = ExtractionReport.score("empty", [])
    assert empty.cases == 0
    assert empty.proposed_recall == 0.0
    assert empty.accepted_precision == 0.0
    assert empty.accepted_recall == 0.0
    assert empty.accepted_f1 == 0.0
    assert empty.grounding_rate == 0.0
    assert empty.metadata_accuracy == 1.0
    assert empty.p50_ms == 0.0
    assert empty.p95_ms == 0.0
    assert empty.wall_ms == 0.0
    assert empty.cases_per_hour == 0.0
    assert empty.backlog_hours == 0.0
    assert empty.render().startswith("empty extraction")


def test_extraction_keeps_model_behavior_failures_inside_the_report() -> None:
    case = ExtractionCase(id="malformed-json", text="source", targets=(target(),))
    benchmark = ExtractionBenchmark(
        StaticExtractor(extraction=UnexpectedModelBehavior("invalid structured response"))
    )

    report = dbutil.run(benchmark.run([case], "model"))

    assert report.failed == 1
    assert report.results[0].error == ("UnexpectedModelBehavior: invalid structured response")


def test_extraction_benchmark_rejects_nonpositive_concurrency() -> None:
    benchmark = ExtractionBenchmark(StaticExtractor(extraction=Extraction(entities=[], facts=[])))

    with pytest.raises(ValueError, match="concurrency must be positive"):
        dbutil.run(benchmark.run_concurrent([], "model", concurrency=0))


def test_load_extraction_cases_reads_nonblank_jsonl(tmp_path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        ExtractionCase(id="one", text="source", targets=(target(),)).model_dump_json() + "\n\n",
        encoding="utf-8",
    )

    cases = load_extraction_cases(path)

    assert len(cases) == 1
    assert cases[0].id == "one"


def test_committed_extraction_corpus_is_bounded_and_loadable() -> None:
    path = Path(__file__).parent / "data" / "extraction_cases.jsonl"

    cases = load_extraction_cases(path)

    assert len(cases) == 16
    assert all(case.text and len(case.targets) <= 2 for case in cases)
