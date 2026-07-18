from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..usage import annotate_transport


class UsageMiddleware:
    """Count one request's transport bytes and stamp them onto its server span.

    The OpenTelemetry Starlette instrumentation opens the root server span but does
    not record body sizes, so this innermost ASGI layer measures the exact bytes it
    relays in each direction and annotates the span for the usage processor.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Relay one connection, measuring HTTP request and response body bytes."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        received = sent = 0

        async def measured_receive() -> Message:
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            return message

        async def measured_send(message: Message) -> None:
            nonlocal sent
            sent += len(message.get("body", b""))
            await send(message)

        await self.app(scope, measured_receive, measured_send)
        annotate_transport(received, sent)
