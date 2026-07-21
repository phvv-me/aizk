import asyncio
import json
from importlib import import_module
from typing import cast

import httpx
import pytest
from doubles import FakeLLM
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from pydantic import BaseModel, SecretStr, ValidationError
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.models import Model
from strategies import wire_extractions

from aizk.config import Settings, settings
from aizk.extract.extractor import Extractor, GLiNERExtractor, LLMExtractor
from aizk.extract.models import (
    BatchConsolidationVerdict,
    Extraction,
)
from aizk.ontology import Ontology, WireEntity, WireExtraction, WireFact
from aizk.serving.extract import LLM, GLiNER, GraphResponse, Relation, Span

extract_client_module = import_module("aizk.serving.extract.client")
extractor_module = import_module("aizk.extract.extractor")


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
    monkeypatch.setattr(
        settings,
        "llm_extra_body",
        {
            "provider": {"zdr": True, "require_parameters": True},
            "reasoning": {"enabled": False},
            "chat_template_kwargs": {"custom_flag": True, "enable_thinking": True},
        },
    )
    schema = WireExtraction
    asyncio.run(
        LLM.from_settings(settings).generate(
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
    assert call.extra_body == {
        "provider": {"zdr": True, "require_parameters": True},
        "reasoning": {"enabled": False},
        "chat_template_kwargs": {"custom_flag": True, "enable_thinking": False},
    }


@given(collection=st.sampled_from(("e", "f")), exceeds=st.booleans())
def test_wire_schemas_bound_graph_expansion_and_preserve_uuid_strings(
    collection: str,
    exceeds: bool,
) -> None:
    schema = WireExtraction.model_json_schema()
    properties = schema["properties"]
    definitions = schema["$defs"]
    assert properties["e"]["maxItems"] == 16
    assert properties["f"]["maxItems"] == 8
    assert definitions["WireEntity"]["properties"]["n"]["maxLength"] == 160
    assert definitions["WireFact"]["properties"]["statement"]["maxLength"] == 384
    limit = 16 if collection == "e" else 8
    item = (
        WireEntity(n="entity", t="concept")
        if collection == "e"
        else WireFact(
            s="subject",
            p="uses",
            statement="subject uses a tool",
            quote="subject uses a tool",
        )
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
        call = LLM.from_settings(settings).generate("s", "u", WireExtraction)
        expected = UnexpectedModelBehavior
    else:
        fake_llm.completions.error = RuntimeError("output budget exhausted")
        call = LLMExtractor(llm=fake_llm.llm).extract("dense source")
        expected = RuntimeError

    with pytest.raises(expected):
        asyncio.run(call)


@given(wire=wire_extractions())
@hyp_settings(deadline=None)
def test_extractor_converts_the_wire_schema(wire: WireExtraction) -> None:
    fake = FakeLLM()
    fake.register(WireExtraction, cast("BaseModel", wire))
    extraction = asyncio.run(LLMExtractor(llm=fake.llm).extract("text"))
    assert [entity.name for entity in extraction.entities] == [entity.n for entity in wire.e]
    assert [entity.type for entity in extraction.entities] == [entity.t for entity in wire.e]
    assert [fact.subject for fact in extraction.facts] == [fact.s for fact in wire.f]
    assert [fact.predicate for fact in extraction.facts] == [fact.p for fact in wire.f]
    assert [fact.object_ for fact in extraction.facts] == [fact.o for fact in wire.f]


def test_extractor_uses_the_live_ontology_prompt_for_every_bounded_source_window(
    fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extractor_module, "chunk_text", lambda text, size: ["first", "second"])

    asyncio.run(LLMExtractor(llm=fake_llm.llm).extract("whole source"))

    assert [call.messages[1]["content"] for call in fake_llm.completions.calls] == [
        "<document>\nfirst\n</document>",
        "<document>\nsecond\n</document>",
    ]
    assert all(
        call.messages[0]["content"] == LLMExtractor.system_prompt()
        and call.max_tokens == settings.llm_extract_max_tokens
        for call in fake_llm.completions.calls
    )


def test_extractor_uses_one_model_turn_for_one_stored_chunk(fake_llm: FakeLLM) -> None:
    source = ("one supported source claim " * 75).strip()
    assert len(source) < settings.chunk_size

    asyncio.run(LLMExtractor(llm=fake_llm.llm).extract(source))

    assert len(fake_llm.completions.calls) == 1
    assert fake_llm.completions.calls[0].messages[1]["content"] == (
        f"<document>\n{source}\n</document>"
    )
    assert fake_llm.completions.calls[0].max_tokens == settings.llm_extract_max_tokens
    assert Settings.model_fields["llm_extract_max_tokens"].default == 2048


def test_extractor_retries_only_context_overflow_as_smaller_spans(
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def generate(
        self: LLM,
        system: str,
        user: str,
        schema: type[BaseModel],
        *,
        temperature: float | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
    ) -> BaseModel:
        calls.append(user)
        if len(calls) == 1:
            raise ModelHTTPError(
                400,
                "extractor",
                {"message": "This model's maximum context length is 3072 tokens."},
            )
        return WireExtraction(e=[], f=[])

    monkeypatch.setattr(LLM, "generate", generate)
    source = ("one supported source claim " * 75).strip()

    extraction = asyncio.run(LLMExtractor(llm=fake_llm.llm).extract(source))

    assert extraction == Extraction(entities=[], facts=[])
    assert len(calls) == 3
    assert all(len(call) < len(calls[0]) for call in calls[1:])


def test_extractor_preserves_an_unsplittable_context_overflow(
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ModelHTTPError(400, "extractor", "maximum context length")
    fake_llm.completions.error = error
    monkeypatch.setattr(extractor_module, "chunk_text", lambda text, size: [text])

    with pytest.raises(ModelHTTPError) as raised:
        asyncio.run(LLMExtractor(llm=fake_llm.llm)._extract_bounded("dense source"))

    assert raised.value is error


@pytest.mark.parametrize(
    "error",
    [
        ModelHTTPError(503, "extractor", {"message": "maximum context length"}),
        ModelHTTPError(400, "extractor", {"message": "invalid guided JSON schema"}),
        ModelHTTPError(400, "extractor", None),
    ],
)
def test_extractor_does_not_split_other_model_http_failures(
    fake_llm: FakeLLM,
    error: ModelHTTPError,
) -> None:
    fake_llm.completions.error = error

    with pytest.raises(ModelHTTPError) as raised:
        asyncio.run(LLMExtractor(llm=fake_llm.llm).extract("dense source"))

    assert raised.value is error


def test_default_extraction_configuration_and_selectable_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings().llm_chat_template_kwargs == {}
    assert Settings().llm_extra_body == {}
    llm = FakeLLM().llm
    gliner = GLiNER.from_settings(settings)
    assert isinstance(Extractor.configured(settings, llm, gliner), LLMExtractor)
    monkeypatch.setattr(settings, "extract_backend", "gliner")
    assert isinstance(Extractor.configured(settings, llm, gliner), GLiNERExtractor)
    monkeypatch.setattr(settings, "extract_backend", "llm")
    assert isinstance(Extractor.configured(settings, llm, gliner), LLMExtractor)


def test_llm_forwards_authenticated_endpoint_headers(
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = (("Modal-Key", "wk-test"), ("Modal-Secret", "ws-test"))
    calls: list[tuple[str, str, str, float, tuple[tuple[str, str], ...]]] = []

    def model(
        url: str,
        api_key: str,
        name: str,
        timeout: float,
        endpoint_headers: tuple[tuple[str, str], ...] = (),
    ) -> Model:
        calls.append((url, api_key, name, timeout, endpoint_headers))
        return fake_llm.model

    monkeypatch.setattr(extract_client_module, "llm_model", model)
    config = settings.model_copy(
        update={
            "llm_headers": {
                "Modal-Secret": SecretStr("ws-test"),
                "Modal-Key": SecretStr("wk-test"),
            }
        }
    )

    LLM.from_settings(config)

    assert calls == [
        (
            config.llm_url,
            config.llm_api_key,
            config.llm_model,
            config.llm_timeout,
            headers,
        )
    ]


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

    extraction = GLiNERExtractor(gliner=GLiNER.from_settings(settings)).convert(text, result)

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

    extraction = GLiNERExtractor(gliner=GLiNER.from_settings(settings)).convert("", result)

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

    extraction = GLiNERExtractor(gliner=GLiNER.from_settings(settings)).convert("", result)
    names = {entity.name for entity in extraction.entities}

    assert len(extraction.entities) == 16
    assert {"Relation head", "Relation tail"} <= names


def test_gliner_extractor_calls_its_service() -> None:
    expected = GraphResponse()

    class FakeGLiNER:
        async def extract(self, text: str) -> GraphResponse:
            assert text == "Ada uses PostgreSQL"
            return expected

    extractor = GLiNERExtractor(gliner=cast("GLiNER", FakeGLiNER()))

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

    result = asyncio.run(GLiNER.from_settings(settings).extract("Ada uses PostgreSQL"))

    assert result == GraphResponse()
    assert requests == [
        {
            "text": "Ada uses PostgreSQL",
            "entity_types": Ontology.current().entity_descriptions,
            "relation_types": Ontology.current().relation_descriptions,
            "threshold": 0.42,
        }
    ]


@pytest.mark.real_services
def test_llm_reuses_its_only_endpoint_client() -> None:
    first = LLM.from_settings(settings)
    second = LLM.from_settings(settings)
    assert first.agent.model is second.agent.model
