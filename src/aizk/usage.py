import asyncio
from collections.abc import Iterable
from contextlib import AbstractContextManager, nullcontext
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from time import perf_counter
from typing import ClassVar
from uuid import uuid4

import asyncpg
from loguru import logger
from opentelemetry import context, propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.starlette import StarletteInstrumentor
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Span, SpanKind, format_span_id, format_trace_id
from pydantic import UUID5, NonNegativeFloat, NonNegativeInt

from .background.queue import Queue, QueueJob, QueuePayload
from .config import settings
from .store import Usage
from .store.engine import Database
from .store.identity import User
from .store.models.tables import UsageEvent
from .store.quota import MonthlyQuota

_OPERATION = "aizk.operation"
_USER = "aizk.user_id"
_ANONYMOUS = "aizk.anonymous"
_TARGETS = "aizk.scopes"
_ITEMS = "aizk.items"
_REQUEST_BYTES = "aizk.request_bytes"
_RESPONSE_BYTES = "aizk.response_bytes"


class UsageContext(QueuePayload):
    """Request-local accounting facts filled by the authenticated operation."""

    user_id: UUID5 | None = None
    anonymous: bool = True
    operation: UsageEvent.Operation | None = None
    targets: tuple[UUID5, ...] = ()
    items: NonNegativeInt = 1


_current: ContextVar[UsageContext | None] = ContextVar("aizk_usage_context", default=None)


def current_context() -> UsageContext:
    """Return the active accounting state or one empty state outside a transport call."""
    return _current.get() or UsageContext()


def accounting_context() -> Token[UsageContext | None]:
    """Isolate one transport call's accounting annotations from every later call."""
    return _current.set(UsageContext())


def annotate_caller(user: User) -> None:
    """Stamp the verified caller onto the current span and accounting context."""
    span = trace.get_current_span()
    span.set_attribute(_USER, str(user.id))
    span.set_attribute(_ANONYMOUS, "true" if user.is_anonymous() else "false")
    _current.set(
        current_context().model_copy(update={"user_id": user.id, "anonymous": user.is_anonymous()})
    )


def annotate_operation(
    operation: UsageEvent.Operation,
    targets: Iterable[UUID5] = (),
    items: int = 1,
) -> None:
    """Stamp the accounted operation, touched scopes, and produced item count."""
    exact_targets = tuple(sorted(set(targets), key=str))
    span = trace.get_current_span()
    span.set_attribute(_OPERATION, operation.value)
    span.set_attribute(_ITEMS, items)
    if exact_targets:
        span.set_attribute(_TARGETS, ",".join(map(str, exact_targets)))
    _current.set(
        current_context().model_copy(
            update={"operation": operation, "targets": exact_targets, "items": items}
        )
    )


def serving_span(name: str) -> AbstractContextManager[Span | None]:
    """Use the active server span or open one for detached MCP request tasks."""
    if trace.get_current_span().is_recording():
        return nullcontext()
    return trace.get_tracer(__name__).start_as_current_span(
        name, context.Context(), kind=SpanKind.SERVER
    )


class UsageCapture(QueuePayload):
    """One successful operation durably admitted to the accounting queue."""

    capture_key: str
    occurred_at: datetime
    user_id: UUID5
    operation: UsageEvent.Operation
    targets: tuple[UUID5, ...]
    request_bytes: NonNegativeInt = 0
    response_bytes: NonNegativeInt = 0
    items: NonNegativeInt = 1
    duration_ms: NonNegativeFloat = 0.0

    def event(self) -> UsageEvent:
        """Build the validated ledger row with stable target and scope ordering."""
        return Usage.Event(
            **self.model_dump(exclude={"occurred_at", "targets", "user_id"}),
            targets=sorted(set(self.targets), key=str),
            created_at=self.occurred_at,
            created_by=self.user_id,
            scopes=[self.user_id],
        )


def capture_usage(
    request_bytes: int,
    response_bytes: int,
    duration_ms: float,
    status_code: int | None = None,
) -> UsageCapture | None:
    """Build one successful identified request's durable accounting payload."""
    span = trace.get_current_span()
    span.set_attribute(_REQUEST_BYTES, request_bytes)
    span.set_attribute(_RESPONSE_BYTES, response_bytes)
    if status_code is not None and status_code >= 400:
        return None
    state = current_context()
    if state.anonymous or state.user_id is None or state.operation is None:
        return None
    span_context = span.get_span_context()
    capture_key = (
        f"{format_trace_id(span_context.trace_id)}:{format_span_id(span_context.span_id)}"
        if span_context.is_valid
        else uuid4().hex
    )
    return UsageCapture(
        capture_key=capture_key,
        occurred_at=datetime.now(UTC),
        user_id=state.user_id,
        operation=state.operation,
        targets=state.targets or (state.user_id,),
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        items=state.items,
        duration_ms=duration_ms,
    )


class UsageAccountingJob(QueueJob[UsageCapture]):
    """Append one captured operation idempotently to the caller's private ledger."""

    entrypoint: ClassVar[str] = "aizk_usage_event"
    payload_type: ClassVar[type[QueuePayload]] = UsageCapture

    async def handle(self, payload: UsageCapture) -> None:
        """Persist the capture once even when PgQueuer reclaims an acknowledged-late job."""
        async with User.private(payload.user_id) as session:
            await session.exec(Usage.Event.capture(payload.event()))


class UsageRecorder:
    """Await durable PgQueuer admission before a successful transport reply is released."""

    def __init__(self, attempts: int = 3, backoff_seconds: float = 0.5) -> None:
        if attempts < 1:
            raise ValueError("usage recording requires at least one attempt")
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds

    async def record(self, capture: UsageCapture) -> None:
        """Persist one deduplicated accounting job, retrying transient queue failures."""
        attempt = 1
        while True:
            try:
                async with Queue(dsn=settings.asyncpg_dsn) as queue:
                    await UsageAccountingJob().enqueue(queue, capture, capture.capture_key)
                return
            except (OSError, TimeoutError, asyncpg.PostgresError) as error:
                if attempt == self.attempts:
                    raise
                logger.warning("usage enqueue attempt {} failed: {}", attempt, error)
                await asyncio.sleep(self.backoff_seconds * attempt)
                attempt += 1


recorder = UsageRecorder()
quota = MonthlyQuota()


async def account_usage(
    request_bytes: int,
    response_bytes: int,
    started_at: float,
    status_code: int | None = None,
) -> None:
    """Durably admit the current successful operation before its transport completes."""
    capture = capture_usage(
        request_bytes,
        response_bytes,
        max(0.0, (perf_counter() - started_at) * 1000),
        status_code,
    )
    if capture is not None:
        await recorder.record(capture)


def observe(database: Database) -> None:
    """Configure local tracing and optional OTLP export for one serving process."""
    provider = TracerProvider(
        sampler=ALWAYS_ON, resource=Resource.create({"service.name": "aizk"})
    )
    if settings.otlp_endpoint is not None:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=str(settings.otlp_endpoint)))
        )
    trace.set_tracer_provider(provider)
    propagate.set_global_textmap(CompositePropagator([]))
    # skip_dep_check because the instrumentor's `sqlalchemy<2.1` pin trips on the
    # env's 2.1 beta and BaseInstrumentor would silently skip instrumentation.
    SQLAlchemyInstrumentor().instrument(
        engine=database.engine.sync_engine, enable_commenter=True, skip_dep_check=True
    )
    HTTPXClientInstrumentor().instrument()
    StarletteInstrumentor().instrument()
