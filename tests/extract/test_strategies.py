import asyncio
from typing import cast

import pytest
from doubles import FakeLLM

from aizk.config import Settings
from aizk.extract.llm.triples import EXTRACTION_SYSTEM
from aizk.extract.models import Extraction
from aizk.extract.ontology import ONTOLOGY_PROMPT
from aizk.extract.strategies import (
    PREFERENCES_SYSTEM,
    SUMMARY_SYSTEM,
    custom_system,
    extract_graph,
)


def last_system(fake_llm: FakeLLM) -> str:
    """The system-prompt content of the most recent recorded turn, typed for comparison.

    fake_llm: the recording double whose last `chat.completions.parse` turn is inspected.
    """
    messages = cast("list[dict[str, str]]", fake_llm.completions.calls[-1]["messages"])
    return messages[0]["content"]


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
    fake_llm: FakeLLM,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    strategy: str,
    system: str,
) -> None:
    """The build path runs the strategy the config names, each carrying its own system prompt.

    Every strategy layers its focus on the shared ontology guidance before the one LLM seam, so
    the recorded system turn is the strategy's own while the span rides through as the user turn.
    """
    monkeypatch.setattr(settings, "extract_strategy", strategy)
    result = asyncio.run(extract_graph("a span of text"))
    assert isinstance(result, Extraction)
    call = fake_llm.completions.calls[-1]
    assert call["messages"] == [
        {"role": "system", "content": system},
        {"role": "user", "content": "a span of text"},
    ]


def test_custom_strategy_layers_its_prompt_on_the_ontology(
    fake_llm: FakeLLM, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filled custom prompt reaches the extraction turn layered on the ontology guidance."""
    monkeypatch.setattr(settings, "extract_strategy", "custom")
    monkeypatch.setattr(settings, "extract_custom_prompt", "prefer terse claims")
    asyncio.run(extract_graph("a span of text"))
    system = last_system(fake_llm)
    assert system.startswith(ONTOLOGY_PROMPT)
    assert "prefer terse claims" in system


def test_custom_without_a_prompt_falls_back_to_the_ontology_default(
    fake_llm: FakeLLM, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selecting custom with no prompt short-circuits to the deterministic ontology extraction."""
    monkeypatch.setattr(settings, "extract_strategy", "custom")
    monkeypatch.setattr(settings, "extract_custom_prompt", "")
    asyncio.run(extract_graph("a span of text"))
    assert last_system(fake_llm) == EXTRACTION_SYSTEM


@pytest.mark.parametrize(
    ("prompt", "layered"),
    [("", False), ("prefer terse claims", True)],
    ids=["bare", "layered"],
)
def test_custom_system_layers_a_prompt_only_when_one_is_configured(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, prompt: str, layered: bool
) -> None:
    """An empty custom prompt is the bare ontology, a filled one layers onto the ontology."""
    monkeypatch.setattr(settings, "extract_custom_prompt", prompt)
    system = custom_system()
    assert system.startswith(ONTOLOGY_PROMPT)
    assert (prompt in system and system != ONTOLOGY_PROMPT) is layered
