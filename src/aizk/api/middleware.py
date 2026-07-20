from collections.abc import Awaitable, Callable
from time import perf_counter

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..usage import account_usage, accounting_context

type AccountUsage = Callable[[int, int, float, int | None], Awaitable[None]]

_STREAM_PATHS = frozenset({"/api/processing/events"})


class UsageMiddleware:
    """Measure one HTTP operation and durably admit its usage before releasing the reply."""

    def __init__(self, app: ASGIApp, account: AccountUsage = account_usage) -> None:
        self.app = app
        self.account = account

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Buffer one bounded API reply until its successful usage event is queued."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") in _STREAM_PATHS:
            with accounting_context():
                await self.app(scope, receive, send)
            return
        received = sent = 0
        status = 500
        messages: list[Message] = []
        started_at = perf_counter()

        async def measured_receive() -> Message:
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            return message

        async def measured_send(message: Message) -> None:
            nonlocal sent, status
            if message["type"] == "http.response.start":
                status = message["status"]
            else:
                sent += len(message.get("body", b""))
            messages.append(message)

        with accounting_context():
            await self.app(scope, measured_receive, measured_send)
            await self.account(received, sent, started_at, status)
        for message in messages:
            await send(message)
