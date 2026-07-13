import asyncio

import pytest
from doubles import FakeLLM

from aizk.config import Settings
from aizk.extract import ontology
from aizk.extract.llm.triples import extraction_system
from aizk.extract.models import Extraction
from aizk.extract.strategies import ExtractionStrategy, extract_graph


def last_system(fake_llm: FakeLLM) -> str:
    return fake_llm.completions.calls[-1].messages[0]["content"]


# Build prompts after the test database refreshes the live ontology.
@pytest.mark.parametrize(
    "strategy",
    [
        ExtractionStrategy.ONTOLOGY,
        ExtractionStrategy.SUMMARY,
        ExtractionStrategy.PREFERENCES,
    ],
)
def test_extract_graph_dispatches_the_selected_strategy_system(
    fake_llm: FakeLLM,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    strategy: ExtractionStrategy,
) -> None:
    monkeypatch.setattr(settings, "extract_strategy", strategy)
    result = asyncio.run(extract_graph("a span of text"))
    assert isinstance(result, Extraction)
    call = fake_llm.completions.calls[-1]
    assert call.messages == [
        {"role": "system", "content": strategy.system()},
        {"role": "user", "content": "a span of text"},
    ]


def test_custom_strategy_layers_its_prompt_on_the_ontology(
    fake_llm: FakeLLM, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "extract_strategy", "custom")
    monkeypatch.setattr(settings, "extract_custom_prompt", "prefer terse claims")
    asyncio.run(extract_graph("a span of text"))
    system = last_system(fake_llm)
    assert system.startswith(ontology.current().prompt)
    assert "prefer terse claims" in system


def test_custom_without_a_prompt_falls_back_to_the_ontology_default(
    fake_llm: FakeLLM, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "extract_strategy", "custom")
    monkeypatch.setattr(settings, "extract_custom_prompt", "")
    asyncio.run(extract_graph("a span of text"))
    assert last_system(fake_llm) == extraction_system()
