import asyncio
import json
from collections.abc import Callable
from importlib import import_module

import httpx
import pytest
from dbutil import run

from aizk.config import Settings, settings
from aizk.ontology import Ontology
from aizk.serving.base import http_client, request_throttle
from aizk.serving.gate import GateClient
from aizk.serving.gate.models import (
    ClassifyRequest,
    ClassifyResponse,
    HealthResponse,
)
from eval.routes import Route

gate_module = import_module("aizk.serving.gate.client")


def gate() -> GateClient:
    return GateClient.from_settings(settings)


def sidecar(monkeypatch: pytest.MonkeyPatch, result: dict) -> list[tuple[str, dict]]:
    """Route the gate client to an in-memory sidecar answering one canned result."""
    requests: list[tuple[str, dict]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=result)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://gliner.test"
    )
    monkeypatch.setattr(settings, "gliner_url", "http://gliner.test")
    monkeypatch.setattr(gate_module, "http_client", lambda *args: client)
    return requests


def test_client_resolves_the_default_and_variant_sidecars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_client.cache_clear()
    monkeypatch.setattr(settings, "gliner_url", "http://gliner.test")
    monkeypatch.setattr(settings, "gliner_variants", {"gliner-relex": "http://relex.test"})

    default = GateClient.from_settings(settings).client
    relex = GateClient.from_settings(settings, "gliner-relex").client

    assert str(default.base_url) == "http://gliner.test/"
    assert str(relex.base_url) == "http://relex.test/"
    assert default.timeout == httpx.Timeout(settings.gliner_timeout)
    assert Settings(_env_file=None).gliner_variants == {}
    http_client.cache_clear()


def test_client_raises_on_an_error_status() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "model fell over"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://gliner.test"
    )
    with pytest.raises(httpx.HTTPStatusError):
        run(
            GateClient(
                client=client,
                throttle=asyncio.Semaphore(1),
                gate_threshold=settings.gliner_gate_threshold,
                gate_floor=settings.gliner_gate_floor,
            ).post("/classify", ClassifyRequest(text="where", tasks={}), ClassifyResponse)
        )


@pytest.mark.parametrize(
    ("result", "classify", "expected", "payload"),
    [
        (
            {"route": Route.LOCAL.value},
            lambda: run(gate().classify("where", "route", Route)),
            Route.LOCAL,
            {
                "text": "where",
                "tasks": {"route": [route.value for route in Route]},
            },
        ),
        (
            {"present": ["Tool", "Project"]},
            lambda: run(
                gate().classify("text", "present", ["Tool", "Project"], multi=True, threshold=0.7)
            ),
            {"Tool", "Project"},
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
        ),
    ],
    ids=["single-enum", "multiple-labels"],
)
def test_classify_preserves_single_and_multi_label_contracts(
    result: dict,
    classify: Callable[[], Route | set[str]],
    expected: Route | set[str],
    payload: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = sidecar(monkeypatch, result)

    assert classify() == expected
    assert requests == [("/classify", payload)]


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
            run(gate().classify("where", "route", Route, multi=True, threshold=0.7))
        else:
            run(gate().classify("where", "route", Route))


def test_named_entities_extracts_normalized_unique_entity_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = sidecar(
        monkeypatch, {"entities": {"Person": [" Ada ", "ada"], "Tool": ["Git", ""]}}
    )

    assert run(gate().named_entities("Ada uses Git")) == ["ada", "git"]
    assert requests == [
        (
            "/extract",
            {
                "text": "Ada uses Git",
                "entity_types": Ontology.current().gate_labels,
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
        client: GateClient,
        text: str,
        task: str,
        labels: list[str],
        *,
        multi: bool = False,
        threshold: float | None = None,
    ) -> set[str]:
        del client
        calls.append((text, task, labels, multi, threshold))
        return present

    monkeypatch.setattr(gate_module.GateClient, "classify", classify)

    assert run(gate().relevant("some text")) is expected
    assert calls == [
        (
            "some text",
            "present",
            Ontology.current().gate_labels,
            True,
            settings.gliner_gate_threshold,
        )
    ]


def test_contract_round_trips_the_sidecar_only_wire_shapes() -> None:
    health = HealthResponse(
        status="ok",
        device="cuda:0",
        checkpoint="fastino/gliner2-large-v1",
    )
    assert health.model_dump() == {
        "status": "ok",
        "device": "cuda:0",
        "checkpoint": "fastino/gliner2-large-v1",
    }
    assert ClassifyResponse.model_validate({"route": None}).label("missing") is None


def test_call_queues_behind_the_per_variant_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gliner_concurrency", 2)
    request_throttle.cache_clear()
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
    monkeypatch.setattr(gate_module, "http_client", lambda *args: client)

    async def burst() -> None:
        request = ClassifyRequest(text="note", tasks={"present": ["Person"]})
        gate = GateClient(
            client=client,
            throttle=request_throttle("http://gliner.test", 2),
            gate_threshold=settings.gliner_gate_threshold,
            gate_floor=settings.gliner_gate_floor,
        )
        async with asyncio.TaskGroup() as group:
            for _ in range(10):
                group.create_task(gate.post("/classify", request, ClassifyResponse))

    run(burst())
    assert peak <= 2
    assert request_throttle("http://gliner.test", 2) is request_throttle("http://gliner.test", 2)
    request_throttle.cache_clear()
