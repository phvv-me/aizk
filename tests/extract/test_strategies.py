import asyncio
from collections.abc import Callable
from typing import cast

import pytest
from doubles import FakeLLM

from aizk.config import Settings
from aizk.extract import ontology
from aizk.extract.llm.triples import extraction_system
from aizk.extract.models import Extraction
from aizk.extract.strategies import (
    custom_system,
    extract_graph,
    preferences_system,
    summary_system,
)


def last_system(fake_llm: FakeLLM) -> str:
    """The system-prompt content of the most recent recorded turn, typed for comparison.

    fake_llm: the recording double whose last `chat.completions.parse` turn is inspected.
    """
    messages = cast("list[dict[str, str]]", fake_llm.completions.calls[-1]["messages"])
    return messages[0]["content"]


# each strategy's own prompt depends on the live ontology catalog, so the parametrize table
# carries the builder function itself, called inside the test body once the suite's bootstrap
# has already refreshed the cache, never the resolved string at collection time.
@pytest.mark.parametrize(
    ("strategy", "build_system"),
    [
        ("ontology", extraction_system),
        ("summary", summary_system),
        ("preferences", preferences_system),
    ],
    ids=["ontology", "summary", "preferences"],
)
def test_extract_graph_dispatches_the_selected_strategy_system(
    fake_llm: FakeLLM,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    strategy: str,
    build_system: Callable[[], str],
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
        {"role": "system", "content": build_system()},
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
    assert system.startswith(ontology.current().prompt)
    assert "prefer terse claims" in system


def test_custom_without_a_prompt_falls_back_to_the_ontology_default(
    fake_llm: FakeLLM, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selecting custom with no prompt short-circuits to the deterministic ontology extraction."""
    monkeypatch.setattr(settings, "extract_strategy", "custom")
    monkeypatch.setattr(settings, "extract_custom_prompt", "")
    asyncio.run(extract_graph("a span of text"))
    assert last_system(fake_llm) == extraction_system()


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
    ontology_prompt = ontology.current().prompt
    assert system.startswith(ontology_prompt)
    assert (prompt in system and system != ontology_prompt) is layered
