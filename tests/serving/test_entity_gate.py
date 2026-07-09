import sys
from types import ModuleType

import pytest
from huggingface_hub.errors import LocalEntryNotFoundError

from aizk.config import settings
from aizk.extract import ontology
from aizk.serving.gate.entity_gate import EntityGate


class FakeModel:
    """A GLiNER2 stand-in returning canned classified types, so `relevant` runs without a model.

    present: the entity kinds `classify_text` reports for every call, in gliner2's own shape.
    """

    def __init__(self, present: list[str]) -> None:
        self.present = present
        self.calls: list[tuple[str, dict[str, object]]] = []

    def classify_text(self, text: str, schema: dict[str, object]) -> dict[str, list[str]]:
        """Record the call and hand back the canned classified types."""
        self.calls.append((text, schema))
        return {"present": self.present}


def build_gate(
    present: list[str], monkeypatch: pytest.MonkeyPatch, downloads: list[bool] | None = None
) -> EntityGate:
    """Construct an `EntityGate` with torch, gliner2, and the HF resolve faked, so no model loads.

    The heavy imports live inside `__init__`, so faking `torch`/`gliner2` in `sys.modules` and
    `snapshot_download` on `huggingface_hub` lets the real constructor run, its thread-cap, offline
    checkpoint resolve, label build, floor, and threshold read, over a `FakeModel` rather than the
    205M checkpoint.

    present: the classified types `FakeModel` reports.
    downloads: per-call `snapshot_download` outcomes, `False` raising `LocalEntryNotFoundError` to
        drive the cold-cache download fallback and `True` returning a path; one warm resolve by
        default.
    """
    fake_torch = ModuleType("torch")
    fake_torch.set_num_threads = lambda n: None
    fake_model = FakeModel(present)

    class GLiNER2:
        @classmethod
        def from_pretrained(cls, name: str, map_location: str) -> FakeModel:
            return fake_model

    fake_gliner = ModuleType("gliner2")
    fake_gliner.GLiNER2 = GLiNER2
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "gliner2", fake_gliner)

    outcomes = iter(downloads if downloads is not None else [True])

    def fake_snapshot_download(model: str, local_files_only: bool = False) -> str:
        if not next(outcomes):
            raise LocalEntryNotFoundError("cold cache")
        return "/cache/gliner2"

    monkeypatch.setattr("huggingface_hub.snapshot_download", fake_snapshot_download)
    gate = EntityGate.__new__(EntityGate)
    gate.__init__()
    return gate


def test_gate_construction_drops_concept_and_reads_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate classifies against extractable types minus Concept, plus the configured floor."""
    gate = build_gate(["Person"], monkeypatch)
    assert ontology.CONCEPT not in gate.labels
    assert "Tool" in gate.labels
    assert gate.floor == settings.gliner_gate_floor
    assert gate.threshold == settings.gliner_gate_threshold


def test_relevant_is_true_when_a_type_beyond_the_floor_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk the head classifies as a substantive type past the floor clears the gate."""
    gate = build_gate(["Person", "Tool"], monkeypatch)
    assert gate.relevant("mentions a specific tool") is True


def test_relevant_is_false_for_floor_only_and_empty_classifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Small talk mapping only onto the floor, or onto nothing, earns no LLM call."""
    assert build_gate(["Person"], monkeypatch).relevant("she walked to the park") is False
    assert build_gate([], monkeypatch).relevant("filler prose") is False


def test_cold_cache_resolve_falls_back_to_a_one_time_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An offline resolve that misses the cache downloads once, then loads that checkpoint."""
    gate = build_gate(["Tool"], monkeypatch, downloads=[False, True])
    assert gate.relevant("names a tool") is True
