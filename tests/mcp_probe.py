import asyncio
import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast

from fastmcp.server.context import Context
from mcp.server.session import ServerSession

from aizk.mcp.middleware import bind_user
from aizk.mcp.server import server
from aizk.store.identity import User

# Complete MCP surface available to an authenticated caller
USER_TOOLS = {
    "recall",
    "remember",
    "reference",
    "share",
}


def text_of(result: object) -> str:
    content = getattr(result, "structured_content", None)
    assert isinstance(content, dict)
    return content["result"]


def const[T](value: T) -> Callable[..., Awaitable[T]]:
    async def fixed(*args: object, **kwargs: object) -> T:
        return value

    return fixed


class Rendered:
    def __init__(self, text: str) -> None:
        self.text = text

    def render(self) -> str:
        return self.text


def context_for(user: User | None = None) -> Context:
    """A request context carrying an already resolved caller, as the middleware leaves it."""
    session = SimpleNamespace(_fastmcp_state_prefix=f"test-{uuid.uuid4()}")
    context = Context(fastmcp=server, session=cast("ServerSession", session))
    if user is not None:
        asyncio.run(bind_user(context, user))
    return context
