import json
import uuid
from importlib import import_module

import dbutil
import httpx
import pytest
import seedgraph
from id_factory import uuid5
from openai import APITimeoutError
from pydantic import UUID5, UUID7

from aizk.config import settings
from aizk.extract.models import ExtractedEntity, Extraction, TimedFact
from aizk.store import Chunk
from eval.gate import GateReport, gated_chunks, measure_gate

gate = import_module("eval.gate")


class StubExtractor:
    def __init__(self, facts: dict[str, int], timeouts: frozenset[str]) -> None:
        self.facts = facts
        self.timeouts = timeouts
        self.calls: list[str] = []

    async def extract(self, text: str) -> Extraction:
        self.calls.append(text)
        label = next((label for label in self.timeouts if label in text), None)
        if label is not None:
            raise APITimeoutError(request=httpx.Request("POST", "http://llm.test/v1"))
        count = next((count for label, count in self.facts.items() if label in text), 0)
        return extraction_with_facts(count)


def chunk_of(text: str, provenance: dict | None = None) -> Chunk:
    return Chunk(
        document_id=uuid.uuid7(),
        ord=0,
        text=text,
        created_by=uuid5(),
        scopes=[settings.system_user_id],
        provenance=provenance or {},
    )


def long_text(label: str) -> str:
    return f"{label} " + "x" * settings.extract_min_chars


def extraction_with_facts(count: int) -> Extraction:
    return Extraction(
        entities=[ExtractedEntity(name="alpha", type="concept")],
        facts=[
            TimedFact(subject="alpha", predicate="related_to", statement=f"fact {index}")
            for index in range(count)
        ],
    )


def install_seams(
    monkeypatch: pytest.MonkeyPatch,
    chunks: list[Chunk],
    accepted: set[str],
    facts: dict[str, int],
    timeouts: frozenset[str] = frozenset(),
) -> StubExtractor:
    """Stub storage and gating, then return the injected extraction service."""

    async def stub_chunks(scopes: frozenset[UUID5 | UUID7], limit: int | None) -> list[Chunk]:
        return chunks[:limit]

    async def stub_relevant(text: str) -> bool:
        return any(label in text for label in accepted)

    monkeypatch.setattr(gate, "gated_chunks", stub_chunks)
    monkeypatch.setattr(gate, "relevant", stub_relevant)
    return StubExtractor(facts, timeouts)


def test_measure_gate_counts_saved_calls_against_recovered_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [
        chunk_of(long_text("kept")),
        chunk_of(long_text("lost"), provenance={"speaker_label": "Ada"}),
        chunk_of(long_text("empty")),
        chunk_of(long_text("slow")),
        chunk_of("tiny"),
    ]
    extractor = install_seams(
        monkeypatch,
        chunks,
        accepted={"kept"},
        facts={"lost": 2},
        timeouts=frozenset({"slow"}),
    )

    report = dbutil.run(measure_gate(extractor=extractor))

    assert report == GateReport(
        chunks=4,
        accepted=1,
        rejected=3,
        rejected_with_facts=1,
        facts_lost=2,
        timed_out=1,
    )
    assert report.positive_rate == 0.25
    assert report.false_negative_rate == pytest.approx(1 / 3)
    assert len(extractor.calls) == 3
    assert any(text.startswith("speaker Ada") for text in extractor.calls)
    dumped = json.loads(report.model_dump_json())
    rendered = report.render()
    assert dumped["positive_rate"] == 0.25
    assert dumped["false_negative_rate"] == pytest.approx(1 / 3)
    assert "gate replay n=4" in rendered
    assert "positive_rate=0.250" in rendered
    assert "false_negative_rate=0.333" in rendered
    assert "facts_lost=2" in rendered
    assert "timed_out=1" in rendered


def test_measure_gate_is_all_zero_rates_on_an_empty_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extractor = install_seams(monkeypatch, [], accepted=set(), facts={})

    report = dbutil.run(measure_gate(scopes=frozenset({uuid5()}), limit=None, extractor=extractor))

    assert report.chunks == 0
    assert report.positive_rate == 0.0
    assert report.false_negative_rate == 0.0


def test_gated_chunks_reads_one_exact_scope_set_in_id_order(migrated_db: None) -> None:
    async def probe() -> tuple[list[str], list[str]]:
        owner = await seedgraph.fresh_owner()
        first = await seedgraph.seed_chunk(owner, "first span")
        del first
        await seedgraph.seed_chunk(owner, "second span")
        await seedgraph.seed_chunk(uuid5(), "foreign span")
        mine = await gated_chunks(frozenset({owner}), None)
        capped = await gated_chunks(frozenset({owner}), 1)
        return [chunk.text for chunk in mine], [chunk.text for chunk in capped]

    mine, capped = dbutil.run(probe())

    assert mine == ["first span", "second span"]
    assert capped == ["first span"]
