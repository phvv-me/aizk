from datetime import UTC, datetime
from time import perf_counter
from types import SimpleNamespace, TracebackType
from typing import cast

import asyncpg
import dbutil
import pytest
from bg_doubles import patch_queue_seam
from id_factory import uuid5
from opentelemetry import propagate, trace
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import SpanKind
from pydantic import UUID5
from sqlalchemy import text

import aizk.usage as usage_mod
from aizk.config import settings
from aizk.store import Usage
from aizk.store.engine import Database
from aizk.store.identity import User
from aizk.usage import (
    UsageAccountingJob,
    UsageCapture,
    UsageRecorder,
    account_usage,
    accounting_context,
    annotate_caller,
    annotate_operation,
    capture_usage,
    current_context,
    observe,
    serving_span,
)


def capture(
    capture_key: str = "trace:span",
    user_id: UUID5 | None = None,
    request_bytes: int = 3,
) -> UsageCapture:
    """One valid capture with deterministic fields for queue and job tests."""
    owner = user_id or uuid5()
    return UsageCapture(
        capture_key=capture_key,
        occurred_at=datetime(2026, 7, 19, 23, 59, tzinfo=UTC),
        user_id=owner,
        operation=Usage.Event.Operation.recall,
        targets=(owner,),
        request_bytes=request_bytes,
        response_bytes=5,
        items=2,
        duration_ms=1.5,
    )


def test_accounting_context_builds_exact_captures_and_never_leaks_state() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("aizk-usage-test")
    owner, team = uuid5(), uuid5()
    user = User.authorized(owner, read=(owner, team), write=(owner,))

    with accounting_context(), tracer.start_as_current_span("request", kind=SpanKind.SERVER):
        annotate_caller(user)
        annotate_operation(Usage.Event.Operation.recall, (team, owner, team), items=7)
        derived = capture_usage(10, 20, 3.5, 200)

    assert derived is not None
    assert derived.user_id == owner
    assert derived.operation is Usage.Event.Operation.recall
    assert derived.targets == tuple(sorted((owner, team), key=str))
    assert derived.items == 7
    assert derived.request_bytes == 10
    assert derived.response_bytes == 20
    assert derived.duration_ms == 3.5
    trace_id, span_id = derived.capture_key.split(":")
    assert (len(trace_id), len(span_id)) == (32, 16)
    assert current_context().user_id is None


@pytest.mark.parametrize("case", ["failed", "anonymous", "unidentified", "unannotated"])
def test_capture_usage_rejects_every_nonbillable_request(case: str) -> None:
    user = User.private(settings.anonymous_user_id if case == "anonymous" else uuid5())
    with accounting_context():
        if case != "unidentified":
            annotate_caller(user)
        if case != "unannotated":
            annotate_operation(Usage.Event.Operation.recall)
        derived = capture_usage(1, 2, 0.0, 500 if case == "failed" else 200)
    assert derived is None


def test_capture_usage_falls_back_to_the_caller_and_random_key_without_a_span() -> None:
    user = User.private(uuid5())
    with accounting_context():
        annotate_caller(user)
        annotate_operation(Usage.Event.Operation.share, frozenset(), items=0)
        derived = capture_usage(0, 0, 0.0)
    assert derived is not None
    assert derived.targets == (user.id,)
    assert derived.items == 0
    assert len(derived.capture_key) == 32


def test_usage_capture_builds_a_complete_row_with_stable_targets() -> None:
    owner, first, second = uuid5(), uuid5(), uuid5()
    payload = capture(user_id=owner).model_copy(update={"targets": (second, first, second)})

    event = payload.event()

    assert event.model_dump(exclude={"id"}) == {
        "capture_key": payload.capture_key,
        "operation": payload.operation,
        "targets": sorted((first, second), key=str),
        "request_bytes": payload.request_bytes,
        "response_bytes": payload.response_bytes,
        "items": payload.items,
        "duration_ms": payload.duration_ms,
        "created_at": payload.occurred_at,
        "created_by": owner,
        "scopes": [owner],
    }


def test_serving_span_reuses_active_server_spans_and_opens_detached_roots() -> None:
    provider = TracerProvider()
    tracer = provider.get_tracer("aizk-serving-span-test")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(usage_mod.trace, "get_tracer", lambda name: tracer)
    try:
        with serving_span("detached") as detached:
            assert detached is not None
        with (
            tracer.start_as_current_span("outer", kind=SpanKind.SERVER),
            serving_span("attached") as attached,
        ):
            assert attached is None
    finally:
        monkeypatch.undo()


def test_usage_accounting_is_idempotent_and_keeps_the_operation_time(
    migrated_db: None,
) -> None:
    owner = uuid5()
    first = capture(capture_key="same", user_id=owner, request_bytes=3)
    changed = first.model_copy(update={"request_bytes": 999})
    second = first.model_copy(update={"capture_key": "other", "request_bytes": 7})

    async def body() -> list[tuple[str, int, datetime]]:
        await dbutil.reset_db()
        job = UsageAccountingJob()
        await job.handle(first)
        await job.handle(changed)
        await job.handle(second)
        async with dbutil.admin_engine().connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT capture_key, request_bytes, created_at "
                    "FROM usage_event ORDER BY capture_key"
                )
            )
            return [tuple(row) for row in rows]

    assert dbutil.run(body()) == [
        ("other", 7, first.occurred_at),
        ("same", 3, first.occurred_at),
    ]


def test_usage_recorder_awaits_one_deduplicated_queue_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = patch_queue_seam(monkeypatch, usage_mod)
    event = capture()

    dbutil.run(UsageRecorder().record(event))
    dbutil.run(UsageRecorder().record(event))

    assert recorder.opened == recorder.closed == 2
    [call] = recorder.enqueues
    assert call.entrypoint == UsageAccountingJob.entrypoint
    assert call.dedupe_key == event.capture_key
    assert UsageCapture.decode(call.payload) == event


def test_usage_recorder_requires_an_attempt() -> None:
    with pytest.raises(ValueError, match="at least one attempt"):
        UsageRecorder(attempts=0)


@pytest.mark.parametrize("failure", [OSError("offline"), TimeoutError(), asyncpg.PostgresError()])
def test_usage_recorder_retries_transient_failures_and_propagates_exhaustion(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    attempts: list[int] = []

    class FailingQueue:
        def __init__(self, *, dsn: str) -> None:
            del dsn

        async def __aenter__(self) -> FailingQueue:
            attempts.append(1)
            raise failure

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None: ...

    monkeypatch.setattr(usage_mod, "Queue", FailingQueue)
    with pytest.raises(type(failure)):
        dbutil.run(UsageRecorder(attempts=2, backoff_seconds=0).record(capture()))
    assert len(attempts) == 2


def test_account_usage_records_only_when_capture_is_billable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[UsageCapture] = []

    class Recorder:
        async def record(self, event: UsageCapture) -> None:
            recorded.append(event)

    monkeypatch.setattr(usage_mod, "recorder", Recorder())
    user = User.private(uuid5())

    async def body() -> None:
        with accounting_context():
            await account_usage(1, 2, perf_counter(), 200)
        with accounting_context():
            annotate_caller(user)
            annotate_operation(Usage.Event.Operation.recall)
            await account_usage(1, 2, perf_counter(), 200)

    dbutil.run(body())
    assert len(recorded) == 1
    assert recorded[0].duration_ms >= 0


@pytest.mark.parametrize("exported", [False, True], ids=["local-only", "otlp"])
def test_observe_installs_tracing_and_optional_export(
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
    assert provider.processors == ([("batch", str(endpoint))] if exported else [])
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
    propagate.set_global_textmap(CompositePropagator([]))
    trace.set_tracer_provider(TracerProvider())
