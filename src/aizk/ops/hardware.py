import asyncio

import httpx
from patos import FrozenModel, FrozenOpenModel
from pydantic import ConfigDict

from ..config import settings

_QUERY_TIMEOUT = 2.0
# The lanes this deployment actually runs. Extraction may be served from outside, in which
# case no local lane answers for it and reporting one as down names a fault that does not
# exist. `ExtractionHealth` already says where extraction is served, so the absence here is
# a lane this deployment does not host rather than a lane that failed.
_LOCAL_LANES = ("vllm-emb", "vllm-rerank")
_EXTRACTION_LANE = "vllm-llm"


def model_lanes() -> tuple[str, ...]:
    """The vLLM lanes this deployment hosts, which excludes extraction when it is external."""
    return _LOCAL_LANES if settings.llm_is_external else (*_LOCAL_LANES, _EXTRACTION_LANE)


# One query per host measurement rather than a combined regex match, so a single field stays
# readable in the merge below instead of a label match against a second series.
_HOST_QUERIES = {
    "load1": "node_load1",
    "load5": "node_load5",
    "load15": "node_load15",
    "memory_total_bytes": "node_memory_MemTotal_bytes",
    "memory_available_bytes": "node_memory_MemAvailable_bytes",
    "disk_total_bytes": 'node_filesystem_size_bytes{mountpoint="/"}',
    "disk_available_bytes": 'node_filesystem_avail_bytes{mountpoint="/"}',
}

# Each query already returns one sample per lane, labeled `service`, since that label is how
# Alloy's own scrape config names the three vLLM targets it already collects.
_LANE_QUERIES = {
    "up": 'up{job="prometheus.scrape.models"}',
    "kv_cache_usage_pct": "vllm:kv_cache_usage_perc * 100",
    "requests_running": "vllm:num_requests_running",
    "requests_waiting": "vllm:num_requests_waiting",
}


class _Sample(FrozenOpenModel):
    """One Prometheus instant-query result sample."""

    metric: dict[str, str]
    value: tuple[float, str]


class _InstantResponse(FrozenOpenModel):
    """One Prometheus instant-query HTTP response envelope.

    The envelope is read for the one field this panel needs, and every other field a metrics
    store chooses to send is ignored rather than refused. VictoriaMetrics returns `status`
    beside `data` and `resultType` inside it, none of which this reads, and forbidding them
    failed every query and reported the whole store unreachable.
    """

    class Data(FrozenOpenModel):
        result: tuple[_Sample, ...] = ()

    data: Data


class ModelLaneLoad(FrozenModel):
    """One vLLM serving lane's scrape reachability and live GPU occupancy.

    Every field but `service` defaults, so the browser API's generated client keeps them
    required rather than optional, since a defaulted field is always present in the response.
    """

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    service: str
    up: bool
    kv_cache_usage_pct: float | None = None
    requests_running: int | None = None
    requests_waiting: int | None = None


class HardwareHealth(FrozenModel):
    """Host CPU, memory, and disk load and per-lane GPU occupancy already collected server-side.

    Read from VictoriaMetrics, which Alloy's built-in unix exporter and the vLLM lanes already
    feed, rather than probed directly with `mainboard`. No aizk process holds NVML device access
    to a GPU, so raw device telemetry (temperature, power, memory used) is not available from any
    process in this deployment; the per-lane KV-cache occupancy and queue depth below are the real
    GPU-adjacent signal that exists today. `reachable` is false whenever `metrics_url` is unset or
    VictoriaMetrics cannot be reached, in which case every measurement stays absent rather than a
    stale or fabricated zero. Every field but `reachable` defaults, so the generated client keeps
    them required rather than optional too.
    """

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    reachable: bool
    load1: float | None = None
    load5: float | None = None
    load15: float | None = None
    memory_total_bytes: int | None = None
    memory_available_bytes: int | None = None
    disk_total_bytes: int | None = None
    disk_available_bytes: int | None = None
    lanes: tuple[ModelLaneLoad, ...] = ()


async def _instant(client: httpx.AsyncClient, query: str) -> tuple[_Sample, ...]:
    """Read one PromQL instant query's result vector."""
    response = await client.get("/api/v1/query", params={"query": query})
    response.raise_for_status()
    return _InstantResponse.model_validate(response.json()).data.result


def _scalar(samples: tuple[_Sample, ...]) -> float | None:
    """Read the first sample's value from one instant query result."""
    return float(samples[0].value[1]) if samples else None


def _rounded(value: float | None) -> int | None:
    """Round one optional PromQL sample to the nearest whole byte or request count."""
    return None if value is None else round(value)


def _lanes(vectors: dict[str, tuple[_Sample, ...]]) -> tuple[ModelLaneLoad, ...]:
    """Merge same-named per-lane samples, keyed by their shared `service` label, into rows."""
    by_service: dict[str, dict[str, float]] = {lane: {} for lane in model_lanes()}
    for field, samples in vectors.items():
        for sample in samples:
            service = sample.metric.get("service")
            if service in by_service:
                by_service[service][field] = float(sample.value[1])
    return tuple(
        ModelLaneLoad(
            service=lane,
            up=bool(values.get("up")),
            kv_cache_usage_pct=values.get("kv_cache_usage_pct"),
            requests_running=_rounded(values.get("requests_running")),
            requests_waiting=_rounded(values.get("requests_waiting")),
        )
        for lane, values in by_service.items()
    )


async def hardware_health() -> HardwareHealth:
    """Read host and model-lane load already collected by the optional observability profile."""
    if settings.metrics_url is None:
        return HardwareHealth(reachable=False)
    try:
        async with httpx.AsyncClient(
            base_url=str(settings.metrics_url), timeout=_QUERY_TIMEOUT
        ) as client:
            host_vectors = await asyncio.gather(
                *(_instant(client, query) for query in _HOST_QUERIES.values())
            )
            lane_vectors = await asyncio.gather(
                *(_instant(client, query) for query in _LANE_QUERIES.values())
            )
    except httpx.HTTPError, ValueError:
        return HardwareHealth(reachable=False)
    host = dict(zip(_HOST_QUERIES, (_scalar(vector) for vector in host_vectors), strict=True))
    return HardwareHealth(
        reachable=True,
        load1=host["load1"],
        load5=host["load5"],
        load15=host["load15"],
        memory_total_bytes=_rounded(host["memory_total_bytes"]),
        memory_available_bytes=_rounded(host["memory_available_bytes"]),
        disk_total_bytes=_rounded(host["disk_total_bytes"]),
        disk_available_bytes=_rounded(host["disk_available_bytes"]),
        lanes=_lanes(dict(zip(_LANE_QUERIES, lane_vectors, strict=True))),
    )
