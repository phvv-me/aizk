import asyncio
import uuid
from contextlib import AbstractContextManager
from typing import cast
from unittest.mock import patch

import pytest
from doubles import FakeChoice, FakeLLM, FakeMessage, FakeParsedCompletion
from hypothesis import given
from pydantic import BaseModel, JsonValue
from strategies import WireExtraction, llm_extractions, predicates

from aizk.config import Settings
from aizk.extract import ontology
from aizk.extract.llm import triples as triples_module
from aizk.extract.llm.client import client_for
from aizk.extract.llm.triples import (
    combined_extract,
    decide_consolidations_batch,
    extract_with_system,
    extraction_system,
    structured,
)
from aizk.extract.models import BatchConsolidationVerdict, ConsolidationVerdict, TimedFact
from aizk.graph.consolidation import FactMatch


# Hypothesis cannot request the function-scoped fake_llm fixture.
def route_pool_at(fake: FakeLLM) -> AbstractContextManager[None]:
    return cast(
        "AbstractContextManager[None]",
        patch.object(triples_module, "client_for", lambda *args, **kwargs: fake),
    )


def existing_match(statement: str) -> FactMatch:
    """One consolidated current-fact projection for a borderline candidate."""
    return FactMatch(
        id=uuid.uuid4(),
        predicate="related_to",
        object_id=None,
        statement=statement,
        distance=0.5,
    )


def test_structured_forwards_prompt_pair_and_sampling(fake_llm: FakeLLM) -> None:
    llm_extraction = ontology.current().llm_extraction
    asyncio.run(
        structured("SYS", "USER", llm_extraction, temperature=0.3, timeout=5.0, max_tokens=99)
    )
    call = fake_llm.completions.calls[-1]
    assert call.messages == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert call.response_model is llm_extraction
    assert (call.temperature, call.timeout, call.max_tokens) == (0.3, 5.0, 99)


def test_structured_raises_when_the_model_parses_nothing(fake_llm: FakeLLM) -> None:
    class NullParse:
        async def parse(
            self,
            *,
            model: str,
            messages: list[dict[str, str]],
            response_format: type[BaseModel],
            temperature: float | None = None,
            timeout: float | None = None,
            max_tokens: int | None = None,
            extra_body: dict[str, JsonValue] | None = None,
        ) -> FakeParsedCompletion:
            return FakeParsedCompletion(choices=[FakeChoice(FakeMessage(None))])

    fake_llm.chat.completions = NullParse()
    with pytest.raises(ValueError, match="no parsed"):
        asyncio.run(structured("s", "u", ontology.current().llm_extraction))


@given(wire=llm_extractions())
def test_extract_with_system_converts_wire_to_domain(wire: WireExtraction) -> None:
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
    fake_llm.register(
        ontology.current().llm_extraction, ontology.current().llm_extraction(e=[], f=[])
    )
    asyncio.run(combined_extract("some text"))
    assert fake_llm.completions.calls[-1].messages[0]["content"] == extraction_system()


def test_decide_consolidations_batch_short_circuits_when_empty(fake_llm: FakeLLM) -> None:
    assert asyncio.run(decide_consolidations_batch([])) == []
    assert fake_llm.completions.calls == []


@given(predicate=predicates)
def test_decide_consolidations_batch_drops_hallucinated_supersedes(predicate: str) -> None:
    fake = FakeLLM()
    existing = existing_match("an existing claim")
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
    existing = existing_match("old")
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
    candidate = TimedFact(subject="s", predicate="uses", statement="new")
    fake_llm.register(BatchConsolidationVerdict, BatchConsolidationVerdict(verdicts=[]))
    verdicts = asyncio.run(decide_consolidations_batch([(candidate, [])]))
    assert verdicts == [ConsolidationVerdict(action="ADD")]


def test_client_is_cached_per_endpoint_tuple() -> None:
    client_for.cache_clear()
    first = client_for("http://pool.test/v1", "m", "k")
    assert client_for("http://pool.test/v1", "m", "k") is first
    assert client_for("http://pool.test/v1", "m2", "k") is not first
    assert client_for("http://pool.test/v1", "m", "k2") is not first
    client_for.cache_clear()


def test_default_settings_carry_no_chat_template_kwargs() -> None:
    assert Settings().llm_chat_template_kwargs in ({}, None, "")
