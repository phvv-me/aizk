from collections import OrderedDict

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitError, TokenBucketRateLimiter
from fastmcp.tools import ToolResult
from pydantic import UUID5

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


class CallerRateLimit(Middleware):
    """Bound every caller to an independent token bucket.

    The verified Aizk user ID is the bucket key. Auth-off and anonymous requests
    therefore share their configured fallback identity while authenticated users
    cannot consume one another's allowance. The cache bounds per-process state.
    """

    def __init__(self, max_requests_per_second: float) -> None:
        self.max_requests_per_second = max_requests_per_second
        self._limiters: OrderedDict[UUID5, TokenBucketRateLimiter] = OrderedDict()

    def limiter(self, user_id: UUID5) -> TokenBucketRateLimiter:
        """Return one bounded five-second burst bucket for a stable caller ID."""
        try:
            limiter = self._limiters.pop(user_id)
        except KeyError:
            limiter = TokenBucketRateLimiter(
                capacity=max(1, round(self.max_requests_per_second * 5)),
                refill_rate=self.max_requests_per_second,
            )
            if len(self._limiters) >= 4096:
                self._limiters.popitem(last=False)
        self._limiters[user_id] = limiter
        return limiter

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        user = await bound_user(request_context(context))
        if user is None:
            raise ToolError("no user resolved before rate limiting")
        if not await self.limiter(user.id).consume():
            raise RateLimitError("caller rate limit exceeded")
        return await call_next(context)
