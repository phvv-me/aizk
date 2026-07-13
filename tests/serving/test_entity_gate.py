import asyncio
import json
from importlib import import_module

import httpx
import pytest
from dbutil import run

from aizk.config import Settings, settings
from aizk.eval.routes import Route
from aizk.extract import ontology
from aizk.serving.gate.contract import (
    ChunkRequest,
    ChunkResponse,
    ClassifyRequest,
    ClassifyResponse,
    HealthResponse,
)

gliner_module = import_module("aizk.serving.gate.gliner")


def sidecar(monkeypatch: pytest.MonkeyPatch, result: dict) -> list[tuple[str, dict]]:
    """Route the gate client to an in-memory sidecar answering one canned result."""
    requests: list[tuple[str, dict]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=result)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://gliner.test"
    )
    monkeypatch.setattr(settings, "gliner_gate_url", "http://gliner.test")
    monkeypatch.setattr(gliner_module, "client", lambda variant="": client)
    return requests


def test_client_resolves_the_default_and_variant_sidecars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gliner_module.client.cache_clear()
    monkeypatch.setattr(settings, "gliner_gate_url", "http://gliner.test")
    monkeypatch.setattr(settings, "gliner_gate_variants", {"gliner-relex": "http://relex.test"})

    default = gliner_module.client()
    relex = gliner_module.client("gliner-relex")

    assert str(default.base_url) == "http://gliner.test"
    assert str(relex.base_url) == "http://relex.test"
    assert default.timeout == httpx.Timeout(settings.gliner_gate_timeout)
    gliner_module.client.cache_clear()


def test_call_raises_on_an_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "model fell over"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://gliner.test"
    )
    monkeypatch.setattr(gliner_module, "client", lambda variant="": client)

    with pytest.raises(httpx.HTTPStatusError):
        run(
            gliner_module.call(
                "/classify", ClassifyRequest(text="where", tasks={}), ClassifyResponse
            )
        )


def test_classify_uses_an_enum_class_as_single_label_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = sidecar(monkeypatch, {"route": Route.LOCAL.value})

    assert run(gliner_module.classify("where", "route", Route)) is Route.LOCAL
    assert requests == [
        ("/classify", {"text": "where", "tasks": {"route": [route.value for route in Route]}})
    ]


def test_classify_uses_the_same_head_for_multiple_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = sidecar(monkeypatch, {"present": ["Tool", "Project"]})

    assert run(
        gliner_module.classify("text", "present", ["Tool", "Project"], multi=True, threshold=0.7)
    ) == {"Tool", "Project"}
    assert requests == [
        (
            "/classify",
            {
                "text": "text",
                "tasks": {
                    "present": {
                        "labels": ["Tool", "Project"],
                        "multi_label": True,
                        "cls_threshold": 0.7,
                    }
                },
            },
        )
    ]


@pytest.mark.parametrize(
    ("result", "multi"),
    [({"route": None}, False), ({"route": ["unknown"]}, True)],
)
def test_classify_rejects_malformed_results(
    result: dict, multi: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar(monkeypatch, result)

    with pytest.raises(ValueError, match="invalid labels"):
        if multi:
            run(gliner_module.classify("where", "route", Route, multi=True, threshold=0.7))
        else:
            run(gliner_module.classify("where", "route", Route))


def test_named_entities_extracts_normalized_unique_entity_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = sidecar(
        monkeypatch, {"entities": {"Person": [" Ada ", "ada"], "Tool": ["Git", ""]}}
    )

    assert run(gliner_module.named_entities("Ada uses Git")) == ["ada", "git"]
    assert requests == [
        (
            "/extract",
            {
                "text": "Ada uses Git",
                "entity_types": ontology.gate_labels(),
                "threshold": settings.gliner_gate_threshold,
            },
        )
    ]


@pytest.mark.parametrize(
    ("present", "expected"),
    [(set(), False), ({"Person"}, False), ({"Person", "Tool"}, True)],
)
def test_relevant_excludes_the_configured_floor(
    present: set[str], expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, list[str], bool, float | None]] = []

    async def classify(
        text: str,
        task: str,
        labels: list[str],
        *,
        multi: bool = False,
        threshold: float | None = None,
    ) -> set[str]:
        calls.append((text, task, labels, multi, threshold))
        return present

    monkeypatch.setattr(gliner_module, "classify", classify)

    assert run(gliner_module.relevant("some text")) is expected
    assert calls == [
        (
            "some text",
            "present",
            ontology.gate_labels(),
            True,
            settings.gliner_gate_threshold,
        )
    ]


def test_settings_default_to_the_bare_gliner2_sidecar_without_variants() -> None:
    assert Settings(_env_file=None).gliner_gate_variants == {}


def test_contract_round_trips_the_sidecar_only_wire_shapes() -> None:
    request = ChunkRequest(text="hello world")
    assert request.model_dump() == {"text": "hello world", "kind": "text", "chunk_size": 2048}
    assert ChunkResponse.model_validate({"spans": ["a", "b"]}).spans == ["a", "b"]
    health = HealthResponse(status="ok", device="cuda:0")
    assert health.model_dump() == {"status": "ok", "device": "cuda:0"}
    assert ClassifyResponse.model_validate({"route": None}).label("missing") is None


def test_call_queues_behind_the_per_variant_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gliner_gate_concurrency", 2)
    gliner_module.throttle.cache_clear()
    in_flight = 0
    peak = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return httpx.Response(200, json={"present": []})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://gliner.test"
    )
    monkeypatch.setattr(gliner_module, "client", lambda variant="": client)

    async def burst() -> None:
        request = ClassifyRequest(text="note", tasks={"present": ["Person"]})
        async with asyncio.TaskGroup() as group:
            for _ in range(10):
                group.create_task(gliner_module.call("/classify", request, ClassifyResponse))

    run(burst())
    assert peak <= 2
    assert gliner_module.throttle() is gliner_module.throttle()
    gliner_module.throttle.cache_clear()
