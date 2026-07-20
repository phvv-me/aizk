import dbutil
import pytest
from starlette.types import Message, Receive, Scope, Send

from aizk.api.middleware import UsageMiddleware


def test_http_reply_waits_for_durable_accounting_with_exact_body_sizes() -> None:
    accounted: list[tuple[int, int, int]] = []
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del scope
        message = await receive()
        assert message["body"] == b"hello"
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"worldwide"})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"hello"}

    async def send(message: Message) -> None:
        sent.append(message)

    async def account(
        request_bytes: int,
        response_bytes: int,
        started_at: float,
        status_code: int | None,
    ) -> None:
        del started_at
        assert sent == []
        accounted.append((request_bytes, response_bytes, status_code or 0))

    dbutil.run(UsageMiddleware(app, account)({"type": "http"}, receive, send))

    assert accounted == [(len(b"hello"), len(b"worldwide"), 200)]
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]


def test_accounting_failure_prevents_a_successful_http_reply() -> None:
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"result"})

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    async def fail(*args: int | float | None) -> None:
        del args
        raise OSError("queue unavailable")

    with pytest.raises(OSError, match="queue unavailable"):
        dbutil.run(UsageMiddleware(app, fail)({"type": "http"}, receive, send))
    assert sent == []


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


def test_event_streams_pass_through_without_response_buffering() -> None:
    relayed: list[str] = []
    accounted = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        relayed.append(scope["path"])
        await send({"type": "http.response.start", "status": 200})

    async def receive() -> Message:
        raise AssertionError("the stream does not read a request body")

    async def send(message: Message) -> None:
        assert message["type"] == "http.response.start"

    async def account(*args: int | float | None) -> None:
        del args
        nonlocal accounted
        accounted = True

    scope = {"type": "http", "path": "/api/processing/events"}
    dbutil.run(UsageMiddleware(app, account)(scope, receive, send))

    assert relayed == ["/api/processing/events"]
    assert not accounted
