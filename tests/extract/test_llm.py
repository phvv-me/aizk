import asyncio
from typing import cast
from unittest.mock import patch

import pytest
from doubles import FakeLLM
from factories import build_live_fact
from hypothesis import given
from pydantic import BaseModel
from strategies import WireExtraction, llm_extractions, predicates

from aizk.config import Settings
from aizk.extract import ontology
from aizk.extract.llm.client import LLMClientPool
from aizk.extract.llm.triples import (
    combined_extract,
    decide_consolidations_batch,
    extract_with_system,
    extraction_system,
    structured,
)
from aizk.extract.models import BatchConsolidationVerdict, ConsolidationVerdict, TimedFact


# the LLM seam is `LLMClientPool.client_for`; a @given property body cannot request the
# function-scoped `fake_llm` fixture, so it routes the pool at its own FakeLLM through this.
def route_pool_at(fake: FakeLLM) -> object:
    """A context manager patching `LLMClientPool.client_for` to hand back one recording double."""
    return patch.object(LLMClientPool, "client_for", lambda self, *a, **k: fake)


def test_structured_forwards_prompt_pair_and_sampling(fake_llm: FakeLLM) -> None:
    """One structured turn sends the system-then-user pair and forwards explicit sampling knobs."""
    llm_extraction = ontology.current().llm_extraction
    asyncio.run(
        structured("SYS", "USER", llm_extraction, temperature=0.3, timeout=5.0, max_tokens=99)
    )
    call = fake_llm.completions.calls[-1]
    assert call["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert call["response_model"] is llm_extraction
    assert (call["temperature"], call["timeout"], call["max_tokens"]) == (0.3, 5.0, 99)


def test_structured_raises_when_the_model_parses_nothing(fake_llm: FakeLLM) -> None:
    """A null `parsed` on the completion is a loud error, never a silently empty return."""
    from doubles import FakeChoice, FakeMessage, FakeParsedCompletion

    class NullParse:
        async def parse(self, **_: object) -> object:
            return FakeParsedCompletion(choices=[FakeChoice(FakeMessage(None))])

    fake_llm.chat.completions = NullParse()
    with pytest.raises(ValueError, match="no parsed"):
        asyncio.run(structured("s", "u", ontology.current().llm_extraction))


@given(wire=llm_extractions())
def test_extract_with_system_converts_wire_to_domain(wire: WireExtraction) -> None:
    """The compact wire schema converts to readable domain shapes, one fact and entity per row."""
    fake = FakeLLM()
    fake.register(ontology.current().llm_extraction, cast("BaseModel", wire))
    with route_pool_at(fake):
        extraction = asyncio.run(extract_with_system("SYS", "text"))
    assert [e.name for e in extraction.entities] == [e.n for e in wire.e]
    assert [e.type for e in extraction.entities] == [e.t for e in wire.e]
    assert [f.subject for f in extraction.facts] == [f.s for f in wire.f]
    assert [f.predicate for f in extraction.facts] == [f.p for f in wire.f]
    assert [f.object_ for f in extraction.facts] == [f.o for f in wire.f]


def test_combined_extract_uses_the_ontology_system_prompt(fake_llm: FakeLLM) -> None:
    """`combined_extract` is `extract_with_system` under the shared ontology extraction prompt."""
    fake_llm.register(
        ontology.current().llm_extraction, ontology.current().llm_extraction(e=[], f=[])
    )
    asyncio.run(combined_extract("some text"))
    assert fake_llm.completions.calls[-1]["messages"][0]["content"] == extraction_system()


def test_decide_consolidations_batch_short_circuits_when_empty(fake_llm: FakeLLM) -> None:
    """No borderline candidates means no LLM call and an empty verdict list."""
    assert asyncio.run(decide_consolidations_batch([])) == []
    assert fake_llm.completions.calls == []


@given(predicate=predicates)
def test_decide_consolidations_batch_drops_hallucinated_supersedes(predicate: str) -> None:
    """An UPDATE naming a supersedes id outside the candidate's own claim set is neutralized."""
    import uuid

    fake = FakeLLM()
    existing = build_live_fact(statement="an existing claim")
    candidate = TimedFact(subject="s", predicate=predicate, statement="new")
    fake.register(
        BatchConsolidationVerdict,
        BatchConsolidationVerdict(
            verdicts=[ConsolidationVerdict(action="UPDATE", supersedes=uuid.uuid4())]
        ),
    )
    with route_pool_at(fake):
        verdicts = asyncio.run(decide_consolidations_batch([(candidate, [existing])]))
    assert verdicts[0].action == "UPDATE"
    assert verdicts[0].supersedes is None  # id not among the candidate's known claims


def test_decide_consolidations_batch_keeps_valid_supersedes(fake_llm: FakeLLM) -> None:
    """An UPDATE naming a real claim id in the candidate's set keeps that supersession."""
    existing = build_live_fact(statement="old")
    candidate = TimedFact(subject="s", predicate="uses", statement="new")
    fake_llm.register(
        BatchConsolidationVerdict,
        BatchConsolidationVerdict(
            verdicts=[ConsolidationVerdict(action="UPDATE", supersedes=existing.id)]
        ),
    )
    verdicts = asyncio.run(decide_consolidations_batch([(candidate, [existing])]))
    assert verdicts[0] == ConsolidationVerdict(action="UPDATE", supersedes=existing.id)


def test_decide_consolidations_batch_defaults_missing_verdict_to_add(fake_llm: FakeLLM) -> None:
    """A candidate the model omitted a verdict for defaults to a safe ADD, aligned by position."""
    candidate = TimedFact(subject="s", predicate="uses", statement="new")
    fake_llm.register(BatchConsolidationVerdict, BatchConsolidationVerdict(verdicts=[]))
    verdicts = asyncio.run(decide_consolidations_batch([(candidate, [])]))
    assert verdicts == [ConsolidationVerdict(action="ADD")]


def test_client_pool_caches_per_endpoint_tuple() -> None:
    """The pool keeps one client per (url, model, key); a new model or key mints a fresh one."""
    pool = LLMClientPool()
    first = pool.client_for("http://pool.test/v1", "m", "k")
    assert pool.client_for("http://pool.test/v1", "m", "k") is first
    assert pool.client_for("http://pool.test/v1", "m2", "k") is not first
    assert pool.client_for("http://pool.test/v1", "m", "k2") is not first


def test_default_settings_carry_no_chat_template_kwargs() -> None:
    """A stock settings load sends no extra_body, so a hosted provider sees no unknown field."""
    assert Settings().llm_chat_template_kwargs in ({}, None, "")
