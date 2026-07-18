import asyncio
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager, nullcontext, suppress
from typing import ClassVar, Self, cast
from uuid import UUID

import asyncpg
from loguru import logger
from opentelemetry import context, propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.starlette import StarletteInstrumentor
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Span, SpanContext, SpanKind, format_span_id
from opentelemetry.util.types import AttributeValue
from pydantic import UUID5, NonNegativeFloat, NonNegativeInt

from .background.queue import Queue, QueueJob, QueuePayload
from .config import settings
from .store import Usage
from .store.engine import Database
from .store.identity import User

_OPERATION = "aizk.operation"
_USER = "aizk.user_id"
_ANONYMOUS = "aizk.anonymous"
_TARGETS = "aizk.scopes"
_REQUEST_BYTES = "aizk.request_bytes"
_RESPONSE_BYTES = "aizk.response_bytes"


def annotate_caller(user: User) -> None:
    """Stamp the verified caller's identity onto the current serving span.

    Identity lives only on the local span, never in OTel baggage, so caller
    identifiers cannot leak into outbound request headers or a later request
    served by the same task.
    """
    span = trace.get_current_span()
    span.set_attribute(_USER, str(user.id))
    span.set_attribute(_ANONYMOUS, "true" if user.is_anonymous() else "false")


def annotate_operation(operation: Usage.Event.Operation, targets: Iterable[UUID5] = ()) -> None:
    """Stamp the accounted operation and the exact scopes it touched on the serving span.

    The operation layer calls this where the touched scopes are known, so usage is
    attributed to the scopes an operation actually read or wrote, never to every
    scope the caller could reach. Without targets the caller becomes the target.
    """
    span = trace.get_current_span()
    span.set_attribute(_OPERATION, operation.value)
    if scopes := sorted(map(str, targets)):
        span.set_attribute(_TARGETS, ",".join(scopes))


def serving_span(name: str) -> AbstractContextManager[Span | None]:
    """The span one transport call annotates, opened here when serving detached it.

    HTTP instrumentation opens the root server span for request-task dispatch, so a
    live recording span passes through untouched. MCP sessions may run a call in a
    task without one, so this opens a detached root server span in that case and the
    usage processor still sees exactly one accounted span per call.
    """
    if trace.get_current_span().is_recording():
        return nullcontext()
    return trace.get_tracer(__name__).start_as_current_span(
        name, context.Context(), kind=SpanKind.SERVER
    )


def annotate_transport(request_bytes: int, response_bytes: int) -> None:
    """Stamp one finished request's payload byte sizes onto its span.

    HTTP layers measure exact body bytes while the MCP layer measures serialized
    message and content sizes, so both counts are semantic payload bytes rather
    than wire bytes. The usage processor accounts only spans carrying these sizes,
    which makes this call the success witness for statusless MCP calls.
    """
    span = trace.get_current_span()
    span.set_attribute(_REQUEST_BYTES, request_bytes)
    span.set_attribute(_RESPONSE_BYTES, response_bytes)


class UsageCapture(QueuePayload):
    """One successful operation derived from a root server span, awaiting accounting.

    Byte sizes are semantic payload bytes as stamped by each transport, not wire
    bytes, and targets are the exact scopes the operation touched.
    """

    key: str
    user_id: UUID5
    operation: Usage.Event.Operation
    targets: tuple[UUID5, ...]
    request_bytes: NonNegativeInt = 0
    response_bytes: NonNegativeInt = 0
    duration_ms: NonNegativeFloat = 0.0

    @staticmethod
    def accounted_operation(
        attributes: Mapping[str, AttributeValue],
    ) -> Usage.Event.Operation | None:
        """The accounted operation of one successful identified request, or nothing.

        Anonymous callers, unannotated spans, and failures derive nothing. The
        transport byte annotation witnesses success for statusless MCP calls and
        HTTP spans are additionally gated by their response status.
        """
        operation = attributes.get(_OPERATION, "")
        if not operation or attributes.get(_ANONYMOUS) != "false":
            return None
        if _REQUEST_BYTES not in attributes:
            return None
        status = attributes.get("http.response.status_code", attributes.get("http.status_code"))
        if status is not None and cast("int", status) >= 400:
            return None
        return Usage.Event.Operation(str(operation))

    @classmethod
    def from_span(cls, span: ReadableSpan) -> Self | None:
        """Derive the capture one finished root server span carries, or nothing.

        A remote parent from an inbound `traceparent` header still leaves the span
        the root of this process, so only spans with a local parent are skipped and
        a client cannot unaccount its requests by sending trace headers.
        """
        if span.kind is not SpanKind.SERVER:
            return None
        if span.parent is not None and not span.parent.is_remote:
            return None
        attributes: Mapping[str, AttributeValue] = span.attributes or {}
        operation = cls.accounted_operation(attributes)
        if operation is None:
            return None
        user_id = UUID(cast("str", attributes[_USER]))
        scopes = cast("str", attributes.get(_TARGETS, ""))
        targets = tuple(UUID(scope) for scope in scopes.split(",") if scope) or (user_id,)
        return cls(
            key=format_span_id(cast("SpanContext", span.get_span_context()).span_id),
            user_id=user_id,
            operation=operation,
            targets=targets,
            request_bytes=cast("int", attributes.get(_REQUEST_BYTES, 0)),
            response_bytes=cast("int", attributes.get(_RESPONSE_BYTES, 0)),
            duration_ms=((span.end_time or 0) - (span.start_time or 0)) / 1e6,
        )


class UsageProcessor(SpanProcessor):
    """Hand every finished root server span's usage capture to one sink."""

    def __init__(self, sink: Callable[[UsageCapture], None]) -> None:
        self.sink = sink

    def on_end(self, span: ReadableSpan) -> None:
        """Derive the span's capture and sink it when the span accounts an operation."""
        capture = UsageCapture.from_span(span)
        if capture is not None:
            self.sink(capture)


class UsageAccountingJob(QueueJob[UsageCapture]):
    """Append one captured operation to the caller's private accounting stream."""

    entrypoint: ClassVar[str] = "aizk_usage_event"
    payload_type: ClassVar[type[QueuePayload]] = UsageCapture

    async def handle(self, payload: UsageCapture) -> None:
        """Persist the capture as one immutable `Usage.Event` row."""
        async with User.private(payload.user_id) as session:
            session.add(
                Usage.Event(
                    operation=payload.operation,
                    targets=sorted(set(payload.targets), key=str),
                    request_bytes=payload.request_bytes,
                    response_bytes=payload.response_bytes,
                    duration_ms=payload.duration_ms,
                    created_by=payload.user_id,
                    scopes=[payload.user_id],
                )
            )


class UsageSink:
    """Durably enqueue captures off the span-end path through one worker task.

    `accept` never blocks a span end. A lazily started worker drains bursts of
    captures over one queue connection each, retries transient database failures a
    bounded number of times, and `drain` flushes everything before shutdown.
    """

    def __init__(
        self, capacity: int = 1024, attempts: int = 3, backoff_seconds: float = 0.5
    ) -> None:
        self.pending: asyncio.Queue[UsageCapture] = asyncio.Queue(capacity)
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        self.worker: asyncio.Task[None] | None = None

    def accept(self, capture: UsageCapture) -> None:
        """Queue one capture on the serving loop, dropping it only when saturated."""
        if self.worker is None or self.worker.done():
            self.worker = asyncio.get_running_loop().create_task(self.run())
        try:
            self.pending.put_nowait(capture)
        except asyncio.QueueFull:
            logger.error("usage sink saturated, dropped capture {}", capture.key)

    async def run(self) -> None:
        """Persist queued captures in bursts until `drain` cancels the worker."""
        while True:
            batch = [await self.pending.get()]
            while not self.pending.empty():
                batch.append(self.pending.get_nowait())
            try:
                await self.persist(batch)
            finally:
                for _ in batch:
                    self.pending.task_done()

    async def persist(self, batch: list[UsageCapture]) -> None:
        """Enqueue one burst over one connection, deduplicated by originating span."""
        job = UsageAccountingJob()
        remaining = list(batch)
        for attempt in range(1, self.attempts + 1):
            try:
                async with Queue(dsn=settings.asyncpg_dsn) as queue:
                    while remaining:
                        await job.enqueue(queue, remaining[0], remaining[0].key)
                        remaining.pop(0)
                return
            except (OSError, TimeoutError, asyncpg.PostgresError) as error:
                logger.warning("usage enqueue attempt {} failed: {}", attempt, error)
                await asyncio.sleep(self.backoff_seconds * attempt)
        logger.error(
            "usage sink dropped {} captures after {} attempts", len(remaining), self.attempts
        )

    async def drain(self) -> None:
        """Flush queued captures and stop the worker, called once at serving shutdown."""
        if self.worker is None:
            return
        await self.pending.join()
        self.worker.cancel()
        with suppress(asyncio.CancelledError):
            await self.worker
        self.worker = None


# One process-wide sink outlives requests, so serving shutdown can drain it.
sink = UsageSink()


def observe(database: Database) -> None:
    """Configure span capture for one serving process, keeping traces in this deployment.

    Installs an always-on tracer provider carrying the usage processor over the
    durable sink, replaces the global propagator with an empty composite so a
    client-sent `traceparent` can never reparent or unsample the accounted server
    spans, instruments Starlette, SQLAlchemy (through the async engine's sync core,
    with SQL comments carrying trace context), and httpx, and optionally exports to
    the OTLP endpoint named by `AIZK_OTLP_ENDPOINT` inside this deployment's own
    network.
    """
    provider = TracerProvider(
        sampler=ALWAYS_ON, resource=Resource.create({"service.name": "aizk"})
    )
    provider.add_span_processor(UsageProcessor(sink.accept))
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
