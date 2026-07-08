import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitError, TokenBucketRateLimiter
from fastmcp.tools.tool import ToolResult

from ..config import settings
from .user import USER_STATE_KEY, User, resolve_user


class IdentityMiddleware(Middleware):
    """Resolve the caller once per call, threading it through Context state to every verb.

    `on_call_tool` resolves the `User` exactly once and stashes it in the request's Context
    state, the one slot `current_user` and every verb body read it back from, so a call pays
    a single bearer-token check rather than one per verb. The whole surface is client verbs a
    key-holder is entitled to reach, so there is nothing to hide from a listing, the operational
    tools that once needed hiding having moved off the server to the CLI.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        user = await resolve_user()
        assert context.fastmcp_context is not None  # every tool call carries a request context
        context.fastmcp_context.set_state(USER_STATE_KEY, user)
        return await call_next(context)


class AnonymousRateLimit(Middleware):
    """Token-bucket rate limiting applied only to unauthenticated tool calls.

    A public group makes the HTTP server readable by strangers, so anonymous tool calls consume
    from one shared token bucket while any authenticated user, keyed or OIDC resolved,
    passes through unthrottled. Composing the bucket rather than subclassing fastmcp's
    RateLimitingMiddleware keeps its inherited on_request hook from also charging every protocol
    handshake and listing, which would drain the bucket before the first tool call arrived. One
    bucket serves all strangers, since an unauthenticated HTTP caller carries no identity to key
    a fairer split on anyway. Registered after `IdentityMiddleware`, so its own `on_call_tool`
    always sees the User already resolved into Context state rather than resolving a second
    one of its own.
    """

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
        assert context.fastmcp_context is not None  # every tool call carries a request context
        user = context.fastmcp_context.get_state(USER_STATE_KEY)
        if not isinstance(user, User) or user.id != settings.anonymous_user_id:
            return await call_next(context)
        if not await self.limiter.consume():
            raise RateLimitError("anonymous rate limit exceeded, authenticate for more")
        return await call_next(context)
