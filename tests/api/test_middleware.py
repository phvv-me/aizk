import dbutil
from id_factory import uuid5
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind, Tracer
from starlette.types import Message, Receive, Scope, Send

from aizk.api.middleware import UsageMiddleware
from aizk.store import Usage
from aizk.store.identity import User
from aizk.usage import UsageCapture, UsageProcessor, annotate_caller, annotate_operation


def capture_pipeline() -> tuple[list[UsageCapture], Tracer]:
    """One isolated tracer whose finished spans land in the returned capture list."""
    captured: list[UsageCapture] = []
    provider = TracerProvider()
    provider.add_span_processor(UsageProcessor(captured.append))
    return captured, provider.get_tracer("aizk-api-transport-test")


def test_http_body_bytes_are_measured_onto_the_server_span() -> None:
    captured, tracer = capture_pipeline()
    user = User.private(uuid5())

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del scope
        annotate_caller(user)
        annotate_operation(Usage.Event.Operation.remember_text, (user.id,))
        message = await receive()
        assert message["body"] == b"hello"
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"worldwide"})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"hello"}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async def body() -> None:
        with tracer.start_as_current_span("POST /api/remember", kind=SpanKind.SERVER) as span:
            span.set_attribute("http.route", "/api/remember")
            span.set_attribute("http.response.status_code", 200)
            await UsageMiddleware(app)({"type": "http"}, receive, send)

    dbutil.run(body())

    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]
    [derived] = captured
    assert derived.operation is Usage.Event.Operation.remember_text
    assert derived.user_id == user.id
    assert derived.request_bytes == len(b"hello")
    assert derived.response_bytes == len(b"worldwide")


def test_non_http_connections_pass_through_unmeasured() -> None:
    relayed: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del receive, send
        relayed.append(scope["type"])

    async def receive() -> Message:
        raise AssertionError("lifespan relays never read a body")

    async def send(message: Message) -> None:
        del message

    dbutil.run(UsageMiddleware(app)({"type": "lifespan"}, receive, send))

    assert relayed == ["lifespan"]
