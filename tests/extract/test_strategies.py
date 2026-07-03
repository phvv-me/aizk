import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from aizk.config import settings
from aizk.extract.llm import triples as triples_module
from aizk.extract.llm.triples import EXTRACTION_SYSTEM
from aizk.extract.models import Extraction
from aizk.extract.ontology import ONTOLOGY_PROMPT
from aizk.extract.strategies import (
    PREFERENCES_SYSTEM,
    SUMMARY_SYSTEM,
    custom_system,
    extract_graph,
)


@dataclass
class RecordingMessage:
    """The `.choices[0].message` shape `structured` reads its `.parsed` reply off of.

    parsed: the empty valid extraction every recorded turn resolves to.
    """

    parsed: Extraction


@dataclass
class RecordingChoice:
    """The `.choices[0]` shape wrapping the recording message, mirroring `ParsedChoice`.

    message: the recording message carrying the fixed reply.
    """

    message: RecordingMessage


@dataclass
class RecordingCompletion:
    """A minimal stand-in for `openai.types.chat.ParsedChatCompletion`.

    choices: always the one recording choice a non-streaming, `n=1` chat completion carries.
    """

    choices: list[RecordingChoice]


@dataclass
class RecordingCompletions:
    """A completions stand-in recording the system prompt each structured turn assembled.

    systems: the system-message content of every turn, in order, the focus a strategy steers by.
    """

    systems: list[str] = field(default_factory=list)

    async def parse(self, **kwargs: object) -> RecordingCompletion:
        """Record the turn's system prompt and return an empty valid extraction.

        kwargs: the model, response_format, and messages the structured turn passed.
        """
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        self.systems.append(str(messages[0]["content"]))
        reply = Extraction(entities=[], facts=[])
        return RecordingCompletion([RecordingChoice(RecordingMessage(reply))])


@dataclass
class RecordingChat:
    """The chat namespace wrapping the recording completions."""

    completions: RecordingCompletions


@dataclass
class RecordingClient:
    """An AsyncOpenAI client stand-in exposing the chat.completions path the seam reaches by."""

    chat: RecordingChat


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> Iterator[RecordingCompletions]:
    """Route every structured turn through a recording client so a test reads the system prompt."""
    completions = RecordingCompletions()
    monkeypatch.setattr(
        triples_module, "client_for", lambda *_, **__: RecordingClient(RecordingChat(completions))
    )
    yield completions


@pytest.mark.parametrize(
    ("strategy", "system"),
    [
        ("ontology", EXTRACTION_SYSTEM),
        ("summary", SUMMARY_SYSTEM),
        ("preferences", PREFERENCES_SYSTEM),
    ],
    ids=["ontology", "summary", "preferences"],
)
def test_extract_graph_dispatches_the_selected_strategy_system(
    llm: RecordingCompletions, strategy: str, system: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build path runs the strategy the config names, each carrying its own system prompt."""
    monkeypatch.setattr(settings, "extract_strategy", strategy)
    extraction = asyncio.run(extract_graph("a span of text"))

    assert isinstance(extraction, Extraction)
    assert llm.systems == [system]


def test_custom_without_a_prompt_falls_back_to_the_ontology_default(
    llm: RecordingCompletions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selecting custom with no prompt sends the same deterministic ontology system prompt."""
    monkeypatch.setattr(settings, "extract_strategy", "custom")
    asyncio.run(extract_graph("a span of text"))

    assert llm.systems == [EXTRACTION_SYSTEM]


def test_a_custom_prompt_reaches_the_extraction_turn(
    llm: RecordingCompletions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The custom strategy layers its prompt on the ontology and sends it as the system message."""
    monkeypatch.setattr(settings, "extract_strategy", "custom")
    monkeypatch.setattr(settings, "extract_custom_prompt", "prefer terse claims")
    asyncio.run(extract_graph("a span of text"))

    assert "prefer terse claims" in llm.systems[0]


@pytest.mark.parametrize(
    ("prompt", "expected_extra"),
    [("", False), ("prefer terse claims", True)],
    ids=["bare", "layered"],
)
def test_custom_system_layers_a_prompt_only_when_one_is_configured(
    prompt: str, expected_extra: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty custom prompt is the bare ontology, a filled one layers onto the ontology."""
    monkeypatch.setattr(settings, "extract_custom_prompt", prompt)
    system = custom_system()

    assert system.startswith(ONTOLOGY_PROMPT)
    assert (prompt in system and system != ONTOLOGY_PROMPT) is expected_extra
