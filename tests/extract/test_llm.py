import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

import pytest
from factories import build_live_fact
from hypothesis import given
from hypothesis import strategies as st
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError
from strategies import (
    extracted_entities,
    extracted_facts,
    extractions,
    short_text,
    timestamp_resolutions,
)
from syrupy.assertion import SnapshotAssertion

from aizk.config import settings
from aizk.extract.llm import client_for, decide_consolidation, extract_triples, resolve_timestamps
from aizk.extract.llm import triples as triples_module
from aizk.extract.llm.triples import EXTRACTION_SYSTEM
from aizk.extract.models import (
    ConsolidationVerdict,
    ExtractedEntity,
    ExtractedFact,
    Extraction,
    FactTimestamp,
    TimestampResolution,
)
from aizk.extract.ontology import ONTOLOGY_PROMPT, EntityType, RelationType

# the union of both closed vocabularies, the membership the off-ontology rejection draws outside of
VOCAB = set(EntityType) | set(RelationType)

# the three consolidation actions typed as the Literal the verdict pins, so a sampled draw is a
# valid action rather than a bare str the model field would reject
ACTIONS: tuple[Literal["ADD", "UPDATE", "NOOP"], ...] = ("ADD", "UPDATE", "NOOP")


@dataclass(frozen=True)
class Call:
    """One recorded `chat.completions.parse` turn, the shape `structured` built and sent.

    response_model: the schema the structured turn asked the response to validate against.
    model: the chat model id the turn carried, the value `settings.llm_model` flows into.
    messages: the system-then-user message pair the turn assembled.
    """

    response_model: type[BaseModel]
    model: str
    messages: list[dict[str, str]]


@dataclass
class DispatchedMessage:
    """The `.choices[0].message` shape `structured` reads its `.parsed` schema instance off of.

    parsed: the staged reply the fake turn resolves to, null when a constrained decode could not
        satisfy the schema, the shape the `structured` guard raises on.
    """

    parsed: BaseModel | None


@dataclass
class DispatchedChoice:
    """The `.choices[0]` shape wrapping the dispatched message, mirroring `ParsedChoice`.

    message: the dispatched message carrying the staged reply.
    """

    message: DispatchedMessage


@dataclass
class DispatchedCompletion:
    """A minimal stand-in for `openai.types.chat.ParsedChatCompletion`.

    choices: always the one dispatched choice a non-streaming, `n=1` chat completion carries.
    """

    choices: list[DispatchedChoice]


@dataclass
class DispatchingCompletions:
    """A completions stand-in that answers each call by the response_format it was asked for.

    The single external seam every extractor flows through, replacing the AsyncOpenAI client at
    `triples.client_for` so a structured turn returns a staged reply with no model or network. It
    records each turn so a property can assert how many passes ran, which schemas they requested,
    and the exact prompt `structured` assembled, the only logic this seam stands in front of.

    replies: the response model type mapped to the instance a matching parse call returns.
    seen: the response model types requested across calls, in order.
    calls: the full recorded turns, in order.
    """

    replies: dict[type[BaseModel], BaseModel] = field(default_factory=dict)
    seen: list[type[BaseModel]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def stage(self, *models: BaseModel) -> None:
        """Arm the seam to answer each model's schema with that instance, clearing prior records.

        models: the replies the next turns return, keyed by their own type.
        """
        self.replies = {type(model): model for model in models}
        self.seen.clear()
        self.calls.clear()

    async def parse(
        self,
        *,
        response_format: type[BaseModel],
        model: str,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
    ) -> DispatchedCompletion:
        """Record the turn and return the staged reply for the requested response_format.

        response_format: schema the caller asked the structured turn to validate against.
        model: chat model id the caller sent.
        messages: the system-then-user message pair the caller assembled.
        temperature: sampling temperature, accepted and ignored.
        timeout: per-call ceiling, accepted and ignored.
        max_tokens: output token cap, accepted and ignored.
        """
        self.seen.append(response_format)
        self.calls.append(Call(response_format, model, messages))
        reply = self.replies[response_format]
        return DispatchedCompletion([DispatchedChoice(DispatchedMessage(reply))])


@dataclass
class DispatchingChat:
    """The chat namespace wrapping the dispatching completions.

    completions: the dispatching completions endpoint.
    """

    completions: DispatchingCompletions


@dataclass
class DispatchingClient:
    """An AsyncOpenAI client stand-in exposing only the `chat.completions.parse` path used here.

    chat: the chat namespace the structured turn reaches through.
    """

    chat: DispatchingChat


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> Iterator[DispatchingCompletions]:
    """Swap the AsyncOpenAI client behind `structured` for a recording dispatcher, for one test.

    Points `triples.client_for` at a dispatching client so every structured turn the extractors run
    flows through the real `structured` body yet returns a staged reply, exercising our prompt
    assembly and verdict gating without a model. A property stages its reply per example with
    `llm.stage(...)`, which also resets the recorded turns.
    """
    completions = DispatchingCompletions()
    client = DispatchingClient(DispatchingChat(completions))
    monkeypatch.setattr(triples_module, "client_for", lambda *_, **__: client)
    yield completions


def test_extracted_fields_annotate_the_ontology_enums_directly() -> None:
    """extracted_entity.type and extracted_fact.predicate carry the ontology enums as their own
    field type, rather than a hand-copied Literal that could drift from them.

    Pydantic renders `EntityType`/`RelationType` as the json-schema enum the endpoint's
    grammar-constrained decoding holds every value inside, so annotating the enum directly is
    what makes drift between the ontology and the extractor's schema structurally impossible
    rather than merely asserted at import time.
    """
    assert ExtractedEntity.model_fields["type"].annotation is EntityType
    assert ExtractedFact.model_fields["predicate"].annotation is RelationType


@given(entity=extracted_entities(), fact=extracted_facts())
def test_in_vocab_models_round_trip(entity: ExtractedEntity, fact: ExtractedFact) -> None:
    """Every vocabulary member builds and survives a dump-then-validate round trip unchanged.

    Sampling across the whole closed vocabulary proves the Literal admits each type and predicate
    the ontology lists, the accept side of the boundary the rejection property guards the other.
    """
    assert entity.type in set(EntityType)
    assert fact.predicate in set(RelationType)
    assert ExtractedEntity.model_validate(entity.model_dump()).type == entity.type
    reborn = ExtractedFact.model_validate(fact.model_dump(by_alias=True))
    assert reborn.predicate == fact.predicate


@given(bogus=st.text().filter(lambda value: value not in VOCAB))
def test_off_vocab_is_rejected(bogus: str) -> None:
    """Any string outside the closed vocabulary fails schema validation for both Literals.

    The lever that makes an off-ontology entity type or predicate impossible at the model boundary,
    so a mistyped value never reaches the graph even past the endpoint's own grammar-constrained
    json-schema enforcement.
    """
    with pytest.raises(ValidationError):
        ExtractedEntity.model_validate({"name": "x", "type": bogus})
    with pytest.raises(ValidationError):
        ExtractedFact.model_validate({"subject": "x", "predicate": bogus, "statement": "y"})


@given(extraction=extractions(), text=short_text)
def test_extract_triples_returns_the_structural_slice(
    llm: DispatchingCompletions, extraction: Extraction, text: str
) -> None:
    """One combined call returns the entity-and-fact slice and assembles the ontology system turn.

    Drives the real `structured` body through the seam, so the asserted prompt pair and model id
    are the request `structured` builds, while the returned slice is the staged reply passed back
    untouched, the single-pass node-and-edge contract.
    """
    llm.stage(extraction)
    result = asyncio.run(extract_triples(text))
    assert result == extraction
    assert llm.seen == [Extraction]
    call = llm.calls[0]
    assert call.model == settings.llm_model
    assert call.messages == [
        {"role": "system", "content": EXTRACTION_SYSTEM},
        {"role": "user", "content": text},
    ]


@given(facts=st.lists(extracted_facts(), min_size=1, max_size=5), data=st.data())
def test_resolve_timestamps_aligns_windows_by_position(
    llm: DispatchingCompletions, facts: list[ExtractedFact], data: st.DataObject
) -> None:
    """The dated pass returns one fact per input, each carrying the window at its own position.

    The bi-temporal alignment invariant over every length relation between the facts and the
    resolved windows: a shorter resolution leaves the tail facts on the always-holding null window,
    a longer one ignores the extra windows, and the structural fields are never touched.
    """
    window_count = data.draw(st.integers(min_value=0, max_value=len(facts) + 2))
    resolution = data.draw(timestamp_resolutions(window_count))
    text = data.draw(short_text)
    llm.stage(resolution)

    dated = asyncio.run(resolve_timestamps(text, facts))

    assert llm.seen == [TimestampResolution]
    assert len(dated) == len(facts)
    stamps = resolution.timestamps
    for index, (timed, fact) in enumerate(zip(dated, facts, strict=True)):
        assert (timed.subject, timed.predicate, timed.object_, timed.statement) == (
            fact.subject,
            fact.predicate,
            fact.object_,
            fact.statement,
        )
        window = stamps[index] if index < len(stamps) else FactTimestamp()
        assert timed.valid_from == window.valid_from
        assert timed.valid_to == window.valid_to


def test_resolve_timestamps_without_facts_makes_no_call(llm: DispatchingCompletions) -> None:
    """With no facts the dated pass returns nothing and never reaches the model."""
    llm.stage()
    dated = asyncio.run(resolve_timestamps("source text", []))
    assert dated == []
    assert llm.seen == []


@given(new=extracted_facts())
def test_decide_consolidation_empty_existing_is_a_trivial_add(
    llm: DispatchingCompletions, new: ExtractedFact
) -> None:
    """Against no existing facts the verdict is ADD and the model is never consulted."""
    llm.stage()
    verdict = asyncio.run(decide_consolidation(new, []))
    assert verdict.action == "ADD"
    assert verdict.supersedes is None
    assert llm.seen == []


@given(new=extracted_facts(), data=st.data())
def test_decide_consolidation_gates_supersedes_to_the_candidate_set(
    llm: DispatchingCompletions, new: ExtractedFact, data: st.DataObject
) -> None:
    """A supersedes id survives only as an UPDATE naming a real candidate, else it is dropped.

    The Mem0 gate that stops a hallucinated id from retiring a real fact. A known id under UPDATE
    is kept, an unknown id is cleared, and ADD or NOOP carry no supersedes whatever the model says.
    """
    ids = data.draw(st.lists(st.uuids(version=5), min_size=1, max_size=4, unique=True))
    existing = [
        build_live_fact(id=fact_id, statement=f"existing {n}") for n, fact_id in enumerate(ids)
    ]
    action = data.draw(st.sampled_from(ACTIONS))
    supersedes = data.draw(st.one_of(st.none(), st.sampled_from(ids), st.uuids(version=5)))
    llm.stage(ConsolidationVerdict(action=action, supersedes=supersedes))

    verdict = asyncio.run(decide_consolidation(new, existing))

    assert llm.seen == [ConsolidationVerdict]
    expected = supersedes if action == "UPDATE" and supersedes in set(ids) else None
    assert verdict.action == action
    assert verdict.supersedes == expected


def test_structured_raises_when_the_model_returns_no_parsed_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completion whose `.parsed` is null is a hard error, never a silent empty extraction.

    Points the seam at a client that returns a well-formed completion carrying `parsed=None`, the
    shape a constrained decode yields when it cannot satisfy the schema, so `structured` fails loud
    with the model id and schema name rather than handing back nothing.
    """

    class NullCompletions:
        """A completions stand-in whose parse always resolves to a `parsed=None` completion."""

        async def parse(self, **_: object) -> DispatchedCompletion:
            return DispatchedCompletion([DispatchedChoice(DispatchedMessage(None))])

    client = DispatchingClient(DispatchingChat(NullCompletions()))
    monkeypatch.setattr(triples_module, "client_for", lambda *_, **__: client)
    with pytest.raises(ValueError, match="returned no parsed Extraction"):
        asyncio.run(triples_module.structured("sys", "user", Extraction))


def test_client_for_is_process_cached() -> None:
    """The AsyncOpenAI client is built once per endpoint and shared, so its httpx pool is reused.

    Our memoization keys on the chat endpoint, model, and key arguments, so a second call passing
    the same three hands back the one patched client built before.
    """
    first = client_for(settings.llm_url, settings.llm_model, settings.llm_api_key)
    second = client_for(settings.llm_url, settings.llm_model, settings.llm_api_key)
    assert first is second
    assert isinstance(first, AsyncOpenAI)


def test_prompts_are_stable(snapshot: SnapshotAssertion) -> None:
    """The ontology prompt and extraction system turn render byte-for-byte as pinned.

    Both are built from the sorted closed vocabularies, so a reorder, a new type, or a reworded
    rule shows here, the temperature-0 reproducibility the whole extraction lane rests on.
    """
    assert snapshot == ONTOLOGY_PROMPT
    assert snapshot == EXTRACTION_SYSTEM
