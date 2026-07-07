import sys
from types import ModuleType

import pytest

from aizk.config import settings
from aizk.extract import ontology
from aizk.serving.gate.entity_gate import EntityGate


class FakeModel:
    """A GLiNER2 stand-in returning a canned entity result, so `relevant` runs without a model.

    result: the `extract_entities` payload every call returns, in gliner2's own shape.
    """

    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls: list[tuple[str, list[str]]] = []

    def extract_entities(
        self, text: str, labels: list[str], include_confidence: bool = True
    ) -> dict[str, object]:
        """Record the call and hand back the canned result the test installed."""
        self.calls.append((text, labels))
        return self.result


def build_gate(result: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> EntityGate:
    """Construct an `EntityGate` with the torch/gliner2 imports faked, so no real model loads.

    The two imports live inside `__init__`, so injecting fake modules into `sys.modules` first lets
    the real constructor run (covering its thread-cap, label build, and threshold read) over a
    `FakeModel` that `from_pretrained` returns, rather than the 205M checkpoint.
    """
    fake_torch = ModuleType("torch")
    fake_torch.set_num_threads = lambda n: None
    fake_model = FakeModel(result)

    class GLiNER2:
        @classmethod
        def from_pretrained(cls, name: str, map_location: str) -> FakeModel:
            return fake_model

    fake_gliner = ModuleType("gliner2")
    fake_gliner.GLiNER2 = GLiNER2
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "gliner2", fake_gliner)
    gate = EntityGate.__new__(EntityGate)
    gate.__init__()
    return gate


def test_gate_construction_drops_concept_from_the_label_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate scores against the extractable entity types minus the Concept catch-all."""
    gate = build_gate({"entities": {}}, monkeypatch)
    assert ontology.CONCEPT not in gate.labels
    assert "Tool" in gate.labels
    assert gate.threshold == settings.gliner_gate_threshold


def test_relevant_is_true_when_a_match_clears_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk naming an ontology entity above the confidence floor clears the gate."""
    high = {"entities": {"Tool": [{"confidence": settings.gliner_gate_threshold + 0.05}]}}
    gate = build_gate(high, monkeypatch)
    assert gate.relevant("mentions a specific tool") is True


def test_relevant_is_false_below_threshold_and_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A low-confidence match or no match at all fails the gate, no LLM call earned."""
    low = {"entities": {"Tool": [{"confidence": settings.gliner_gate_threshold - 0.2}]}}
    assert build_gate(low, monkeypatch).relevant("weak signal") is False
    assert build_gate({"entities": {}}, monkeypatch).relevant("filler prose") is False
