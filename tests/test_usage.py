import asyncio
from types import SimpleNamespace, TracebackType
from typing import cast

import pytest
from bg_doubles import patch_queue_seam
from id_factory import uuid5, uuid7
from opentelemetry import trace
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, TraceFlags, Tracer
from pydantic import UUID5, UUID7

import aizk.memory as memory_module
import aizk.usage as usage_mod
from aizk.artifacts import ArtifactIntake
from aizk.config import settings
from aizk.memory import Memory
from aizk.retrieval import Candidate, Lane
from aizk.store import Usage
from aizk.store.engine import Database
from aizk.store.identity import OrganizationStanding, User
from aizk.usage import (
    UsageCapture,
    UsageProcessor,
    UsageSink,
    annotate_caller,
    annotate_operation,
    annotate_transport,
    observe,
)


def capture_pipeline() -> tuple[list[UsageCapture], Tracer]:
    """One isolated tracer whose finished spans land in the returned capture list."""
    captured: list[UsageCapture] = []
    provider = TracerProvider()
    provider.add_span_processor(UsageProcessor(captured.append))
    return captured, provider.get_tracer("aizk-usage-test")


def capture(**overrides: object) -> UsageCapture:
    """One valid capture with deterministic fields for queue and job tests."""
    user_id = uuid5()
    fields: dict[str, object] = {
        "key": "0011223344556677",
        "user_id": user_id,
        "operation": Usage.Event.Operation.recall,
        "targets": (user_id,),
        "request_bytes": 3,
        "response_bytes": 5,
        "duration_ms": 1.5,
    }
    return UsageCapture.model_validate(fields | overrides)


def test_annotated_mcp_style_root_span_derives_the_full_capture() -> None:
    captured, tracer = capture_pipeline()
    user_id, org = uuid5(), uuid5()
    user = User.authorized(user_id, read=(user_id, org), write=(user_id,))

    async def body() -> None:
        with tracer.start_as_current_span("POST /mcp", kind=SpanKind.SERVER):
            annotate_caller(user)
            annotate_operation(Usage.Event.Operation.recall, (user_id, org))
            annotate_transport(10, 20)

    asyncio.run(body())

    [derived] = captured
    assert derived.operation is Usage.Event.Operation.recall
    assert derived.user_id == user_id
    assert derived.targets == tuple(sorted((user_id, org), key=str))
    assert derived.request_bytes == 10
    assert derived.response_bytes == 20
    assert derived.duration_ms >= 0
    assert len(derived.key) == 16


def test_a_client_sent_remote_parent_cannot_unaccount_the_server_span() -> None:
    captured, tracer = capture_pipeline()
    user = User.private(uuid5())
    remote = trace.set_span_in_context(
        NonRecordingSpan(
            SpanContext(
                trace_id=0x1,
                span_id=0x2,
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
        )
    )

    async def body() -> None:
        with tracer.start_as_current_span(
            "POST /api/recall", context=remote, kind=SpanKind.SERVER
        ) as span:
            span.set_attribute("http.response.status_code", 200)
            annotate_caller(user)
            annotate_operation(Usage.Event.Operation.recall)
            annotate_transport(3, 4)

    asyncio.run(body())

    [derived] = captured
    assert derived.operation is Usage.Event.Operation.recall
    assert derived.targets == (user.id,)


def test_usage_accounting_ignores_the_anonymous_public_reader() -> None:
    captured, tracer = capture_pipeline()
    anonymous = User.private(settings.anonymous_user_id)

    async def body() -> None:
        with tracer.start_as_current_span("POST /mcp", kind=SpanKind.SERVER):
            annotate_caller(anonymous)
            annotate_operation(Usage.Event.Operation.recall)
            annotate_transport(1, 2)

    asyncio.run(body())

    assert captured == []


def test_failed_unfinished_child_and_unidentified_spans_derive_nothing() -> None:
    captured, tracer = capture_pipeline()
    user = User.private(uuid5())

    async def failed_request() -> None:
        with tracer.start_as_current_span("POST /api/recall", kind=SpanKind.SERVER) as span:
            span.set_attribute("http.status_code", 500)
            annotate_caller(user)
            annotate_operation(Usage.Event.Operation.recall)
            annotate_transport(1, 2)

    async def failed_mcp_call_without_transport_bytes() -> None:
        with tracer.start_as_current_span("POST /mcp", kind=SpanKind.SERVER):
            annotate_caller(user)
            annotate_operation(Usage.Event.Operation.recall)

    async def unaccounted_route() -> None:
        with tracer.start_as_current_span("GET /api/me", kind=SpanKind.SERVER):
            annotate_caller(user)
            annotate_transport(1, 2)

    async def internal_span() -> None:
        with tracer.start_as_current_span("worker pass"):
            annotate_caller(user)
            annotate_operation(Usage.Event.Operation.recall)
            annotate_transport(1, 2)

    async def child_span() -> None:
        with (
            tracer.start_as_current_span("outer", kind=SpanKind.SERVER),
            tracer.start_as_current_span("inner", kind=SpanKind.SERVER),
        ):
            annotate_caller(user)
            annotate_operation(Usage.Event.Operation.recall)
            annotate_transport(1, 2)

    async def unidentified_caller() -> None:
        with tracer.start_as_current_span("POST /mcp", kind=SpanKind.SERVER):
            annotate_operation(Usage.Event.Operation.recall)
            annotate_transport(1, 2)

    for case in (
        failed_request,
        failed_mcp_call_without_transport_bytes,
        unaccounted_route,
        internal_span,
        child_span,
        unidentified_caller,
    ):
        asyncio.run(case())

    assert captured == []


def test_an_operation_without_targets_falls_back_to_the_caller_as_target() -> None:
    captured, tracer = capture_pipeline()
    scopeless = User.authorized(uuid5())

    async def body() -> None:
        with tracer.start_as_current_span("POST /mcp", kind=SpanKind.SERVER):
            annotate_caller(scopeless)
            annotate_operation(Usage.Event.Operation.share, frozenset())
            annotate_transport(0, 0)

    asyncio.run(body())

    [derived] = captured
    assert derived.targets == (scopeless.id,)


def test_memory_remember_classifies_text_and_targets_only_the_write_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, tracer = capture_pipeline()
    owner, org = uuid5(), uuid5()
    user = User.authorized(owner, read=(owner, org), write=(owner,))

    async def ingest(user: User, text: str, **context: object) -> UUID7:
        del user, text, context
        return uuid7()

    async def queue(document_id: UUID7, scopes: frozenset[UUID5]) -> int:
        del document_id, scopes
        return 1

    monkeypatch.setattr(memory_module.extract_ingest, "ingest_text", ingest)
    monkeypatch.setattr(memory_module, "enqueue_document", queue)
    memory = Memory(user=user, intake=cast("ArtifactIntake", None))

    async def body() -> None:
        with tracer.start_as_current_span("POST /mcp", kind=SpanKind.SERVER):
            annotate_caller(user)
            await memory.remember("a durable note")
            annotate_transport(14, 0)

    asyncio.run(body())

    [derived] = captured
    assert derived.operation is Usage.Event.Operation.remember_text
    assert derived.targets == (owner,)


def test_memory_recall_targets_only_the_scopes_present_in_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, tracer = capture_pipeline()
    owner, org, unrelated = uuid5(), uuid5(), uuid5()
    user = User.authorized(
        owner,
        read=(owner, org, unrelated),
        write=(owner,),
        organizations=(
            OrganizationStanding(
                id=org,
                name="Lab",
                roles=("viewer",),
                permissions=("read:memory",),
            ),
        ),
    )
    evidence = [
        Candidate(lane=Lane.Kind.FACTS, line="a fact", scopes=frozenset({owner})),
        Candidate(lane=Lane.Kind.FACTS, line="an org fact", scopes=frozenset({org})),
    ]

    async def stub(query: str, user: User, token_budget: int | None = None) -> list[Candidate]:
        del query, user, token_budget
        return evidence

    monkeypatch.setattr(memory_module.retrieval, "recall", stub)
    memory = Memory(user=user, intake=cast("ArtifactIntake", None))

    async def body() -> None:
        with tracer.start_as_current_span("POST /mcp", kind=SpanKind.SERVER):
            annotate_caller(user)
            await memory.recall("what holds", 100)
            annotate_transport(10, 20)

    asyncio.run(body())

    [derived] = captured
    assert derived.operation is Usage.Event.Operation.recall
    assert derived.targets == tuple(sorted((owner, org), key=str))


def test_sink_persists_a_burst_once_per_span_key_over_one_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = patch_queue_seam(monkeypatch, usage_mod)
    event = capture()

    asyncio.run(UsageSink().persist([event, event]))

    assert (recorder.opened, recorder.closed) == (1, 1)
    [call] = recorder.enqueues
    assert call.entrypoint == "aizk_usage_event"
    assert call.dedupe_key == event.key
    assert UsageCapture.decode(call.payload) == event


def test_sink_accepts_on_the_serving_loop_and_drain_flushes_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = patch_queue_seam(monkeypatch, usage_mod)
    sink = UsageSink()
    first, second = capture(key="000000000000000a"), capture(key="000000000000000b")

    async def body() -> None:
        await sink.drain()  # draining before any capture is a no-op
        sink.accept(first)
        sink.accept(second)  # the running worker is reused within one burst
        await sink.drain()
        sink.accept(capture(key="000000000000000c"))  # a drained worker restarts
        await sink.drain()

    asyncio.run(body())

    assert [call.dedupe_key for call in recorder.enqueues] == [
        first.key,
        second.key,
        "000000000000000c",
    ]
    assert sink.worker is None


def test_sink_drops_a_capture_when_saturated() -> None:
    sink = UsageSink(capacity=1)

    async def body() -> None:
        sink.worker = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
        try:
            sink.accept(capture(key="000000000000000d"))
            sink.accept(capture(key="000000000000000e"))
        finally:
            sink.worker.cancel()

    asyncio.run(body())

    assert sink.pending.qsize() == 1


def test_sink_retries_transient_failures_then_drops_the_burst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    class FailingQueue:
        def __init__(self, *, dsn: str) -> None:
            del dsn

        async def __aenter__(self) -> FailingQueue:
            attempts.append(1)
            raise OSError("connection refused")

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None: ...

    monkeypatch.setattr(usage_mod, "Queue", FailingQueue)
    sink = UsageSink(attempts=2, backoff_seconds=0)

    asyncio.run(sink.persist([capture()]))

    assert len(attempts) == 2


@pytest.mark.parametrize("exported", [False, True], ids=["local-only", "otlp"])
def test_observe_installs_the_tracer_provider_and_instruments_every_layer(
    monkeypatch: pytest.MonkeyPatch, exported: bool
) -> None:
    calls: dict[str, object] = {}

    class RecordingProvider:
        def __init__(self, *, sampler: object, resource: Resource) -> None:
            calls["sampler"] = sampler
            calls["resource"] = resource
            self.processors: list[object] = []

        def add_span_processor(self, processor: object) -> None:
            self.processors.append(processor)

    monkeypatch.setattr(usage_mod, "TracerProvider", RecordingProvider)
    monkeypatch.setattr(
        usage_mod.trace,
        "set_tracer_provider",
        lambda provider: calls.setdefault("provider", provider),
    )
    monkeypatch.setattr(
        usage_mod.propagate,
        "set_global_textmap",
        lambda propagator: calls.setdefault("propagator", propagator),
    )
    monkeypatch.setattr(
        usage_mod.SQLAlchemyInstrumentor,
        "instrument",
        lambda self, **kwargs: calls.setdefault("sqlalchemy", kwargs),
    )
    monkeypatch.setattr(
        usage_mod.HTTPXClientInstrumentor,
        "instrument",
        lambda self: calls.setdefault("httpx", True),
    )
    monkeypatch.setattr(
        usage_mod.StarletteInstrumentor,
        "instrument",
        lambda self: calls.setdefault("starlette", True),
    )
    monkeypatch.setattr(usage_mod, "OTLPSpanExporter", lambda endpoint: endpoint)
    monkeypatch.setattr(usage_mod, "BatchSpanProcessor", lambda exporter: ("batch", exporter))
    endpoint = "http://tempo:4318/v1/traces" if exported else None
    monkeypatch.setattr(settings, "otlp_endpoint", endpoint)
    database = cast("Database", SimpleNamespace(engine=SimpleNamespace(sync_engine="SYNC")))

    observe(database)

    provider = cast("RecordingProvider", calls["provider"])
    assert calls["sampler"] is ALWAYS_ON
    assert cast("Resource", calls["resource"]).attributes["service.name"] == "aizk"
    assert isinstance(provider.processors[0], UsageProcessor)
    assert provider.processors[0].sink == usage_mod.sink.accept
    if exported:
        assert provider.processors[1:] == [("batch", str(endpoint))]
    else:
        assert provider.processors[1:] == []
    propagator = cast("CompositePropagator", calls["propagator"])
    assert isinstance(propagator, CompositePropagator)
    assert propagator.fields == set()
    assert calls["sqlalchemy"] == {
        "engine": "SYNC",
        "enable_commenter": True,
        "skip_dep_check": True,
    }
    assert calls["httpx"] is True
    assert calls["starlette"] is True
