from types import TracebackType
from typing import Self

import dbutil
import httpx
import pytest
from pydantic.networks import AnyHttpUrl

import aizk.ops as ops
from aizk.config import settings
from aizk.ops import hardware


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self.payload


def fake_client(series: dict[str, list[dict]]) -> type:
    """Build one `httpx.AsyncClient` stand-in answering each configured PromQL query."""

    class Client:
        def __init__(self, *, base_url: str, timeout: float) -> None:
            self.base_url = base_url
            self.timeout = timeout

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool:
            del exc_type, exc, traceback
            return False

        async def get(self, path: str, params: dict[str, str]) -> FakeResponse:
            assert path == "/api/v1/query"
            # The envelope a real store sends, `status` beside `data` and `resultType` inside
            # it, neither of which this reads. A fixture carrying only the read fields let a
            # response model that forbade the rest pass every test and fail every real query.
            return FakeResponse(
                {
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": series.get(params["query"], []),
                    },
                }
            )

    return Client


def broken_client(error: Exception) -> type:
    """Build one `httpx.AsyncClient` stand-in whose every request raises `error`."""

    class Client:
        def __init__(self, *, base_url: str, timeout: float) -> None:
            del base_url, timeout

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool:
            del exc_type, exc, traceback
            return False

        async def get(self, path: str, params: dict[str, str]) -> FakeResponse:
            del path, params
            raise error

    return Client


def sample(value: str, **labels: str) -> dict:
    """Build one Prometheus instant-query sample with a fixed timestamp."""
    return {"metric": labels, "value": [1786068905.0, value]}


def test_hardware_health_is_absent_when_metrics_are_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "metrics_url", None)
    monkeypatch.setattr(
        hardware.httpx,
        "AsyncClient",
        lambda **_: (_ for _ in ()).throw(AssertionError("must not dial an unconfigured store")),
    )

    health = dbutil.run(ops.hardware_health())

    assert health == ops.HardwareHealth(reachable=False)


def test_hardware_health_reads_host_and_lane_series(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "metrics_url", AnyHttpUrl("http://victoriametrics:8428"))
    series = {
        hardware._HOST_QUERIES["load1"]: [sample("0.5")],
        hardware._HOST_QUERIES["load5"]: [sample("0.75")],
        hardware._HOST_QUERIES["load15"]: [sample("1.0")],
        hardware._HOST_QUERIES["memory_total_bytes"]: [sample("270104739840")],
        hardware._HOST_QUERIES["memory_available_bytes"]: [sample("200000000000")],
        hardware._HOST_QUERIES["disk_total_bytes"]: [sample("900000000000")],
        hardware._HOST_QUERIES["disk_available_bytes"]: [sample("400000000000")],
        hardware._LANE_QUERIES["up"]: [
            sample("1", service="vllm-emb"),
            sample("1", service="vllm-rerank"),
            sample("0", service="vllm-llm"),
        ],
        hardware._LANE_QUERIES["kv_cache_usage_pct"]: [
            sample("12.5", service="vllm-emb"),
            sample("0", service="vllm-rerank"),
        ],
        hardware._LANE_QUERIES["requests_running"]: [sample("2", service="vllm-emb")],
        hardware._LANE_QUERIES["requests_waiting"]: [sample("0", service="vllm-emb")],
    }
    monkeypatch.setattr(hardware.httpx, "AsyncClient", fake_client(series))

    health = dbutil.run(ops.hardware_health())

    assert health.reachable is True
    assert (health.load1, health.load5, health.load15) == (0.5, 0.75, 1.0)
    assert health.memory_total_bytes == 270104739840
    assert health.memory_available_bytes == 200000000000
    assert health.disk_total_bytes == 900000000000
    assert health.disk_available_bytes == 400000000000
    lanes = {lane.service: lane for lane in health.lanes}
    assert lanes["vllm-emb"] == ops.ModelLaneLoad(
        service="vllm-emb",
        up=True,
        kv_cache_usage_pct=12.5,
        requests_running=2,
        requests_waiting=0,
    )
    assert lanes["vllm-rerank"].up is True
    assert lanes["vllm-rerank"].kv_cache_usage_pct == 0.0
    assert lanes["vllm-rerank"].requests_running is None
    assert lanes["vllm-llm"] == ops.ModelLaneLoad(service="vllm-llm", up=False)


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), ValueError("bad json")],
    ids=["network-error", "malformed-response"],
)
def test_hardware_health_degrades_to_unreachable_on_any_failure(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(settings, "metrics_url", AnyHttpUrl("http://victoriametrics:8428"))
    monkeypatch.setattr(hardware.httpx, "AsyncClient", broken_client(error))

    health = dbutil.run(ops.hardware_health())

    assert health == ops.HardwareHealth(reachable=False)


def test_hardware_health_ignores_envelope_fields_it_does_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A metrics store may send whatever else it likes beside the one field this reads.

    VictoriaMetrics returns `status`, `resultType` and per-sample extras. Refusing them made
    every query raise, which the caller turned into one honest-looking `reachable=False`, so
    the panel reported the whole store unreachable while it was answering every request.
    """
    monkeypatch.setattr(settings, "metrics_url", AnyHttpUrl("http://victoriametrics:8428"))
    generous = {hardware._HOST_QUERIES["load1"]: [{**sample("0.5"), "unexpected": "ignored"}]}
    monkeypatch.setattr(hardware.httpx, "AsyncClient", fake_client(generous))

    health = dbutil.run(ops.hardware_health())

    assert health.reachable is True
    assert health.load1 == 0.5


@pytest.mark.parametrize(
    ("llm_url", "expected"),
    [
        ("http://vllm-llm:8000/v1", ("vllm-emb", "vllm-rerank", "vllm-llm")),
        ("https://openrouter.ai/api/v1", ("vllm-emb", "vllm-rerank")),
    ],
    ids=["local-extraction", "external-extraction"],
)
def test_model_lanes_name_only_what_this_deployment_hosts(
    monkeypatch: pytest.MonkeyPatch, llm_url: str, expected: tuple[str, ...]
) -> None:
    """A lane served from outside is absent rather than reported down.

    Extraction moved to a hosted provider, so no local lane answers for it. Listing it anyway
    made the console report a permanent failure for a service this deployment never runs.
    """
    monkeypatch.setattr(settings, "llm_url", llm_url)

    assert hardware.model_lanes() == expected


def test_hardware_lanes_omit_extraction_when_it_is_served_from_outside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "metrics_url", AnyHttpUrl("http://victoriametrics:8428"))
    monkeypatch.setattr(settings, "llm_url", "https://openrouter.ai/api/v1")
    series = {
        hardware._LANE_QUERIES["up"]: [
            sample("1", service="vllm-emb"),
            sample("1", service="vllm-rerank"),
            sample("0", service="vllm-llm"),
        ]
    }
    monkeypatch.setattr(hardware.httpx, "AsyncClient", fake_client(series))

    health = dbutil.run(ops.hardware_health())

    assert {lane.service for lane in health.lanes} == {"vllm-emb", "vllm-rerank"}
