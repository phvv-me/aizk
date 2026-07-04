from collections.abc import Sequence

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitError, TokenBucketRateLimiter
from fastmcp.tools.tool import Tool, ToolResult

from ..config import settings
from .principal import ADMIN_TAG, PRINCIPAL_STATE_KEY, Principal, resolve_principal


class PrincipalMiddleware(Middleware):
    """Resolve the caller once per call, threading it through Context state and hiding admin tools.

    `on_call_tool` resolves the `Principal` exactly once and stashes it in the request's Context
    state, the one slot `current_principal` and every tool body read it back from, so a call no
    longer pays a bearer-token check and an is_admin query per tool plus another per gate.
    `on_list_tools` resolves its own copy since a listing carries no tool call of its own to stash
    onto, then hides the ADMIN_TAG tools from a non-admin listing so a regular user neither sees
    nor is tempted to call any of them. The admin gate itself lives on each admin tool through
    `AizkMCP.admin_tool`, not here, a listing hides a tool without enforcing access to it, and a
    caller could still reach a hidden tool directly through `tool.run()`.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        principal = await resolve_principal()
        assert context.fastmcp_context is not None  # every tool call carries a request context
        context.fastmcp_context.set_state(PRINCIPAL_STATE_KEY, principal)
        return await call_next(context)

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        principal = await resolve_principal()
        return tools if principal.is_admin else [t for t in tools if ADMIN_TAG not in t.tags]


class AnonymousRateLimit(Middleware):
    """Token-bucket rate limiting applied only to unauthenticated tool calls.

    A public group makes the HTTP server readable by strangers, so anonymous tool calls consume
    from one shared token bucket while any authenticated principal, keyed or Zitadel resolved,
    passes through unthrottled. Composing the bucket rather than subclassing fastmcp's
    RateLimitingMiddleware keeps its inherited on_request hook from also charging every protocol
    handshake and listing, which would drain the bucket before the first tool call arrived. One
    bucket serves all strangers, since an unauthenticated HTTP caller carries no identity to key
    a fairer split on anyway. Registered after `PrincipalMiddleware`, so its own `on_call_tool`
    always sees the Principal already resolved into Context state rather than resolving a second
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
        principal = context.fastmcp_context.get_state(PRINCIPAL_STATE_KEY)
        if not isinstance(principal, Principal) or principal.id != settings.anonymous_principal_id:
            return await call_next(context)
        if not await self.limiter.consume():
            raise RateLimitError("anonymous rate limit exceeded, authenticate for more")
        return await call_next(context)
