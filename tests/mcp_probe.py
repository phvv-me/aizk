import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast

import dbutil
from fastmcp.server.context import Context
from fastmcp.tools import FunctionTool
from id_factory import uuid5
from mcp.server.session import ServerSession
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind

from aizk.artifacts.service import ArtifactIntake
from aizk.artifacts.uploads import UploadBox
from aizk.auth import Auth
from aizk.config import settings
from aizk.mcp.middleware import IdentityMiddleware, bind_user
from aizk.mcp.server import AizkMCP
from aizk.runtime import Runtime
from aizk.storage import ByteStore
from aizk.store.identity import User
from aizk.usage import UsageAccountingJob, UsageCapture, UsageProcessor

# Complete MCP surface available to an authenticated caller
USER_TOOLS = {
    "recall",
    "remember",
    "share",
    "status",
}

# One assembled service graph backs every probe server, exactly like an entrypoint.
runtime = Runtime.assemble(settings)


def build_server(
    intake: ArtifactIntake | None = None,
    store: ByteStore | None = None,
    uploads: UploadBox | None = None,
    name: str = "aizk",
) -> AizkMCP:
    """Construct one MCP server over the probe runtime with optional fake dependencies."""
    return AizkMCP(
        runtime.auth,
        store if store is not None else runtime.store,
        uploads if uploads is not None else runtime.uploads,
        intake if intake is not None else runtime.artifacts.intake,
        settings,
        name=name,
    )


def tools_of(application: AizkMCP) -> dict[str, FunctionTool]:
    """Index a server's registered tools by name for direct invocation in tests."""
    registered = dbutil.run(application.list_tools())
    assert all(isinstance(tool, FunctionTool) for tool in registered)
    return {tool.name: tool for tool in registered if isinstance(tool, FunctionTool)}


server = build_server()


def context_for(user: User | None = None) -> Context:
    """A request context carrying an already resolved caller, as the middleware leaves it."""
    session = SimpleNamespace(_fastmcp_state_prefix=f"test-{uuid5()}")
    context = Context(fastmcp=server, session=cast("ServerSession", session))
    if user is not None:
        asyncio.run(bind_user(context, user))
    return context


# Every probe transport span ends in this list as its derived usage capture.
captured: list[UsageCapture] = []
provider = TracerProvider()
provider.add_span_processor(UsageProcessor(captured.append))
tracer = provider.get_tracer("aizk-test-transport")


class StubAuth:
    """Auth double resolving one fixed caller for transport probes."""

    def __init__(self, user: User) -> None:
        self.user = user

    async def resolve(self) -> User:
        return self.user


def transport_middleware(user: User) -> IdentityMiddleware:
    """The real identity middleware bound to one already verified caller."""
    return IdentityMiddleware(cast("Auth", StubAuth(user)))


async def through_transport[ResultT](call: Callable[[], Awaitable[ResultT]]) -> ResultT:
    """Run one MCP call inside the root server span HTTP serving would open."""
    with tracer.start_as_current_span("POST /mcp", kind=SpanKind.SERVER):
        return await call()


async def drain_usage() -> None:
    """Persist every captured usage event exactly as the queue worker would."""
    job = UsageAccountingJob()
    while captured:
        await job.handle(captured.pop(0))
