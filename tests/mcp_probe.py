import asyncio
from types import SimpleNamespace
from typing import cast

from fastmcp.server.context import Context
from id_factory import uuid5
from mcp.server.session import ServerSession

from aizk.mcp.middleware import bind_user
from aizk.mcp.server import AizkMCP
from aizk.store.identity import User

# Complete MCP surface available to an authenticated caller
USER_TOOLS = {
    "recall",
    "remember",
    "share",
    "status",
}

server = AizkMCP.shared()


def context_for(user: User | None = None) -> Context:
    """A request context carrying an already resolved caller, as the middleware leaves it."""
    session = SimpleNamespace(_fastmcp_state_prefix=f"test-{uuid5()}")
    context = Context(fastmcp=server, session=cast("ServerSession", session))
    if user is not None:
        asyncio.run(bind_user(context, user))
    return context
