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
            return FakeResponse({"data": {"result": series.get(params["query"], [])}})

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
