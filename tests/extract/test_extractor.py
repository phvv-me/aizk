import asyncio
import json
from contextlib import AbstractContextManager
from importlib import import_module
from typing import cast
from unittest.mock import patch

import httpx
import pytest
from doubles import FakeLLM
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid7
from pydantic import BaseModel, ValidationError
from pydantic_ai.exceptions import UnexpectedModelBehavior
from strategies import predicates, wire_extractions

import aizk.serving.extract.client as llm_module
from aizk.config import Settings, settings
from aizk.extract.extractor import Extractor, GLiNERExtractor, LLMExtractor
from aizk.extract.models import (
    BatchConsolidationVerdict,
    ConsolidationVerdict,
    TimedFact,
)
from aizk.graph.consolidation import Consolidator, FactMatch
from aizk.ontology import Ontology, WireEntity, WireExtraction, WireFact
from aizk.serving.extract import LLM, GLiNER, GraphResponse, Relation, Span

extract_client_module = import_module("aizk.serving.extract.client")
extractor_module = import_module("aizk.extract.extractor")


def route_llm_to(fake: FakeLLM) -> AbstractContextManager[None]:
    return cast(
        "AbstractContextManager[None]",
        patch.object(llm_module, "llm_model", lambda *args: fake.model),
    )


def existing_match(statement: str) -> FactMatch:
    return FactMatch(
        id=uuid7(),
        object_id=None,
        statement=statement,
        distance=0.5,
    )


@pytest.mark.parametrize(
    ("max_tokens", "expected_tokens"),
    [(99, 99), (None, settings.llm_response_max_tokens)],
    ids=["explicit", "default"],
)
def test_llm_forwards_the_typed_generation_contract(
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    max_tokens: int | None,
    expected_tokens: int,
) -> None:
    monkeypatch.setattr(settings, "llm_chat_template_kwargs", {"enable_thinking": False})
    schema = WireExtraction
    asyncio.run(
        LLM.configured().generate(
            "SYS",
            "USER",
            schema,
            temperature=0.3,
            timeout=5.0,
            max_tokens=max_tokens,
        )
    )
    call = fake_llm.completions.calls[-1]
    assert call.messages == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USER"},
    ]
    assert call.response_model is schema
    assert (call.temperature, call.timeout, call.max_tokens) == (0.3, 5.0, expected_tokens)
    assert call.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


@given(collection=st.sampled_from(("e", "f")), exceeds=st.booleans())
def test_wire_schemas_bound_graph_expansion_and_preserve_uuid_strings(
    collection: str,
    exceeds: bool,
) -> None:
    schema = WireExtraction.model_json_schema()
    properties = schema["properties"]
    definitions = schema["$defs"]
    assert properties["e"]["maxItems"] == 8
    assert properties["f"]["maxItems"] == 4
    assert definitions["WireEntity"]["properties"]["n"]["maxLength"] == 160
    assert definitions["WireFact"]["properties"]["statement"]["maxLength"] == 384
    limit = 8 if collection == "e" else 4
    item = (
        WireEntity(n="entity", t="concept")
        if collection == "e"
        else WireFact(s="subject", p="uses", statement="subject uses a tool")
    )
    payload = {"e": [], "f": [], collection: [item] * (limit + int(exceeds))}
    if exceeds:
        with pytest.raises(ValidationError):
            WireExtraction.model_validate(payload)
    else:
        WireExtraction.model_validate(payload)

    consolidation = BatchConsolidationVerdict.model_json_schema()
    supersedes = consolidation["$defs"]["ConsolidationVerdict"]["properties"]["supersedes"]
    assert supersedes["anyOf"] == [{"type": "string"}, {"type": "null"}]


@pytest.mark.parametrize("failure", ["invalid-json", "model-error"])
def test_extraction_surfaces_model_contract_failures(fake_llm: FakeLLM, failure: str) -> None:
    if failure == "invalid-json":
        fake_llm.completions.raw = "not valid JSON"
        call = LLM.configured().generate("s", "u", WireExtraction)
        expected = UnexpectedModelBehavior
    else:
        fake_llm.completions.error = RuntimeError("output budget exhausted")
        call = LLMExtractor().extract("dense source")
        expected = RuntimeError

    with pytest.raises(expected):
        asyncio.run(call)


@given(wire=wire_extractions())
def test_extractor_converts_the_wire_schema(wire: WireExtraction) -> None:
    fake = FakeLLM()
    fake.register(WireExtraction, cast("BaseModel", wire))
    with route_llm_to(fake):
        extraction = asyncio.run(LLMExtractor().extract("text"))
    assert [entity.name for entity in extraction.entities] == [entity.n for entity in wire.e]
    assert [entity.type for entity in extraction.entities] == [entity.t for entity in wire.e]
    assert [fact.subject for fact in extraction.facts] == [fact.s for fact in wire.f]
    assert [fact.predicate for fact in extraction.facts] == [fact.p for fact in wire.f]
    assert [fact.object_ for fact in extraction.facts] == [fact.o for fact in wire.f]


def test_extractor_uses_the_live_ontology_prompt_for_every_bounded_source_window(
    fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extractor_module, "chunk_text", lambda text, size: ["first", "second"])

    asyncio.run(LLMExtractor().extract("whole source"))

    assert [call.messages[1]["content"] for call in fake_llm.completions.calls] == [
        "<document>\nfirst\n</document>",
        "<document>\nsecond\n</document>",
    ]
    assert all(
        call.messages[0]["content"] == LLMExtractor.system_prompt()
        and call.max_tokens == settings.llm_extract_max_tokens
        for call in fake_llm.completions.calls
    )


def test_default_extraction_configuration_and_selectable_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings().llm_chat_template_kwargs == {}
    assert isinstance(Extractor.configured(), LLMExtractor)
    monkeypatch.setattr(settings, "extract_backend", "gliner")
    assert isinstance(Extractor.configured(), GLiNERExtractor)
    monkeypatch.setattr(settings, "extract_backend", "llm")
    assert isinstance(Extractor.configured(), LLMExtractor)


def test_gliner_extractor_builds_grounded_facts_and_closes_relation_entities() -> None:
    text = "Ada uses PostgreSQL. Ada lives in London."
    ada = Span(text="Ada", start=0, end=3, confidence=0.99)
    postgres = Span(
        text="PostgreSQL",
        start=text.index("PostgreSQL"),
        end=text.index("PostgreSQL") + len("PostgreSQL"),
        confidence=0.95,
    )
    london = Span(
        text="London",
        start=text.index("London"),
        end=text.index("London") + len("London"),
        confidence=0.9,
    )
    second_ada = Span(
        text="Ada",
        start=text.index("Ada", 1),
        end=text.index("Ada", 1) + len("Ada"),
        confidence=0.98,
    )
    result = GraphResponse(
        entities={"person": [ada], "tool": [postgres]},
        relation_extraction={
            "uses": [Relation(head=ada, tail=postgres)],
            "lives_in": [Relation(head=second_ada, tail=london)],
            "authored_by": [Relation(head=ada, tail=second_ada)],
        },
    )

    extraction = GLiNERExtractor().convert(text, result)

    assert {(entity.name, entity.type) for entity in extraction.entities} == {
        ("Ada", "person"),
        ("PostgreSQL", "tool"),
        ("London", "concept"),
    }
    assert [(fact.subject, fact.predicate, fact.object_) for fact in extraction.facts] == [
        ("Ada", "uses", "PostgreSQL"),
        ("Ada", "lives_in", "London"),
    ]
    assert [fact.quote for fact in extraction.facts] == [
        "Ada uses PostgreSQL.",
        "Ada lives in London.",
    ]


def test_gliner_extractor_falls_back_to_a_relation_sentence_without_source_grounding() -> None:
    head = Span(text="Ada", start=0, end=3, confidence=0.8)
    tail = Span(text="Git", start=4, end=7, confidence=0.7)
    result = GraphResponse(relation_extraction={"uses": [Relation(head=head, tail=tail)]})

    extraction = GLiNERExtractor().convert("", result)

    assert extraction.facts[0].statement == "Ada uses Git."
    assert extraction.facts[0].quote is None


def test_gliner_extractor_reserves_its_entity_budget_for_relation_endpoints() -> None:
    standalone = [
        Span(text=f"Entity {index}", start=0, end=1, confidence=1.0 - index / 100)
        for index in range(16)
    ]
    head = Span(text="Relation head", start=0, end=1, confidence=0.7)
    tail = Span(text="Relation tail", start=2, end=3, confidence=0.7)
    result = GraphResponse(
        entities={"concept": standalone},
        relation_extraction={"uses": [Relation(head=head, tail=tail)]},
    )

    extraction = GLiNERExtractor().convert("", result)
    names = {entity.name for entity in extraction.entities}

    assert len(extraction.entities) == 16
    assert {"Relation head", "Relation tail"} <= names


def test_gliner_extractor_calls_its_service() -> None:
    expected = GraphResponse()

    class FakeGLiNER:
        async def extract(self, text: str) -> GraphResponse:
            assert text == "Ada uses PostgreSQL"
            return expected

    extractor = GLiNERExtractor(cast("GLiNER", FakeGLiNER()))

    assert extractor.requires_gate is False
    assert asyncio.run(extractor.extract("Ada uses PostgreSQL")) == extractor.convert(
        "Ada uses PostgreSQL", expected
    )


def test_gliner_client_sends_the_live_ontology_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"entities": {}, "relation_extraction": {}})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        base_url="http://gliner.test",
    )
    monkeypatch.setattr(settings, "gliner_url", "http://gliner.test")
    monkeypatch.setattr(settings, "gliner_extract_threshold", 0.42)
    monkeypatch.setattr(extract_client_module, "http_client", lambda *args: client)

    result = asyncio.run(GLiNER.configured().extract("Ada uses PostgreSQL"))

    assert result == GraphResponse()
    assert requests == [
        {
            "text": "Ada uses PostgreSQL",
            "entity_types": Ontology.current().entity_descriptions,
            "relation_types": Ontology.current().relation_descriptions,
            "threshold": 0.42,
        }
    ]


def test_consolidator_short_circuits_an_empty_batch(fake_llm: FakeLLM) -> None:
    assert asyncio.run(Consolidator().resolve([])) == []
    assert fake_llm.completions.calls == []


@given(predicate=predicates)
def test_consolidator_drops_a_hallucinated_supersedes(predicate: str) -> None:
    fake = FakeLLM()
    existing = existing_match("an existing claim")
    candidate = TimedFact(subject="s", predicate=predicate, statement="new")
    fake.register(
        BatchConsolidationVerdict,
        BatchConsolidationVerdict(
            verdicts=[ConsolidationVerdict(action="UPDATE", supersedes=uuid7())]
        ),
    )
    with route_llm_to(fake):
        verdicts = asyncio.run(Consolidator().resolve([(candidate, [existing])]))
    assert verdicts[0].action == "UPDATE"
    assert verdicts[0].supersedes is None


@pytest.mark.parametrize("response", ["known-update", "missing-verdict"])
def test_consolidator_normalizes_model_verdicts(fake_llm: FakeLLM, response: str) -> None:
    existing = existing_match("old")
    candidate = TimedFact(subject="s", predicate="uses", statement="new")
    verdicts = (
        [ConsolidationVerdict(action="UPDATE", supersedes=existing.id)]
        if response == "known-update"
        else []
    )
    fake_llm.register(BatchConsolidationVerdict, BatchConsolidationVerdict(verdicts=verdicts))
    matches = [existing] if response == "known-update" else []
    expected = verdicts or [ConsolidationVerdict(action="ADD")]

    assert asyncio.run(Consolidator().resolve([(candidate, matches)])) == expected


def test_llm_reuses_its_only_endpoint_client() -> None:
    first = LLM.configured()
    second = LLM.configured()
    assert first.agent.model is second.agent.model
