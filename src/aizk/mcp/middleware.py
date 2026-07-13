import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitError, TokenBucketRateLimiter
from fastmcp.tools import ToolResult

from ..store.identity import User
from .auth import Auth

_USER_STATE = "aizk_user"


async def bind_user(context: Context, user: User) -> None:
    """Stash the resolved caller on the request context for every verb to read."""
    await context.set_state(_USER_STATE, user, serializable=False)


async def bound_user(context: Context) -> User | None:
    """The caller stashed on the request context, null before identity resolution."""
    user = await context.get_state(_USER_STATE)
    return user if isinstance(user, User) else None


def request_context(context: MiddlewareContext[mt.CallToolRequestParams]) -> Context:
    """The FastMCP request context a middleware stashes state on, required to exist."""
    if context.fastmcp_context is None:
        raise ToolError("tool call carries no request context")
    return context.fastmcp_context


class IdentityMiddleware(Middleware):
    """Resolve the caller once per call and stash it on the request context for every verb."""

    def __init__(self, auth: Auth) -> None:
        self.auth = auth

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        await bind_user(request_context(context), await self.auth.resolve())
        return await call_next(context)


class AnonymousRateLimit(Middleware):
    """Token-bucket rate limiting applied only to unauthenticated tool calls."""

    def __init__(self, max_requests_per_second: float) -> None:
        # the bucket holds a five-second burst so a stranger's short read sequence flows while the
        # sustained rate stays capped at the configured requests per second
        self.limiter = TokenBucketRateLimiter(
            capacity=max(1, round(max_requests_per_second * 5)),
            refill_rate=max_requests_per_second,
        )

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        user = await bound_user(request_context(context))
        if user is not None and not user.is_anonymous():
            return await call_next(context)
        if not await self.limiter.consume():
            raise RateLimitError("anonymous rate limit exceeded, authenticate for more")
        return await call_next(context)
