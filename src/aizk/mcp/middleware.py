from collections import OrderedDict
from collections.abc import Callable

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.resources import ResourceResult
from fastmcp.server.context import Context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitError, TokenBucketRateLimiter
from fastmcp.tools import ToolResult
from pydantic import UUID5, BaseModel

from ..auth import Auth
from ..store import Usage
from ..store.identity import User
from ..usage import annotate_caller, annotate_operation, annotate_transport, serving_span

_USER_STATE = "aizk_user"


def tool_reply_size(result: ToolResult) -> int:
    """Total serialized content bytes of one tool reply, a semantic payload size.

    This measures the JSON-serialized content blocks, not wire bytes, so it omits
    the JSON-RPC envelope and counts binary data at its base64-expanded size.
    """
    return sum(len(block.model_dump_json().encode()) for block in result.content)


def resource_reply_size(result: ResourceResult) -> int:
    """Total raw content bytes one resource reply carries, a semantic payload size."""
    return sum(
        len(item.content) if isinstance(item.content, bytes) else len(str(item.content).encode())
        for item in result.contents
    )


async def bind_user(context: Context, user: User) -> None:
    """Stash the resolved caller on the request context for every verb to read."""
    await context.set_state(_USER_STATE, user, serializable=False)


async def bound_user(context: Context) -> User | None:
    """The caller stashed on the request context, null before identity resolution."""
    user = await context.get_state(_USER_STATE)
    return user if isinstance(user, User) else None


def request_context[ParamsT](context: MiddlewareContext[ParamsT]) -> Context:
    """The FastMCP request context a middleware stashes state on, required to exist."""
    if context.fastmcp_context is None:
        raise ToolError("the call carries no request context")
    return context.fastmcp_context


class IdentityMiddleware(Middleware):
    """Resolve the caller once per request and account the transport work it carries.

    Tool calls and resource reads run through the same resolution, so the artifact
    resource is bound to a verified caller exactly like every tool. The caller is
    stamped onto the serving span here, operations and touched scopes are stamped
    by the operation layer itself, and each successful call adds its semantic
    request and reply byte sizes for the usage processor.
    """

    def __init__(self, auth: Auth) -> None:
        self.auth = auth

    async def resolve[ParamsT: BaseModel, ResultT](
        self,
        context: MiddlewareContext[ParamsT],
        call_next: CallNext[ParamsT, ResultT],
        operation: Usage.Event.Operation | None,
        reply_size: Callable[[ResultT], int],
    ) -> ResultT:
        """Bind the freshly resolved caller, run the handler, and account its transport."""
        user = await self.auth.resolve()
        await bind_user(request_context(context), user)
        with serving_span(f"mcp {context.method or 'request'}"):
            annotate_caller(user)
            if operation is not None:
                annotate_operation(operation)
            request_bytes = len(context.message.model_dump_json(exclude_none=True).encode())
            result = await call_next(context)
            annotate_transport(request_bytes, reply_size(result))
            return result

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        return await self.resolve(context, call_next, None, tool_reply_size)

    async def on_read_resource(
        self,
        context: MiddlewareContext[mt.ReadResourceRequestParams],
        call_next: CallNext[mt.ReadResourceRequestParams, ResourceResult],
    ) -> ResourceResult:
        return await self.resolve(
            context,
            call_next,
            Usage.Event.Operation.artifact_read,
            resource_reply_size,
        )


class CallerRateLimit(Middleware):
    """Bound every caller to an independent token bucket.

    The verified Aizk user ID is the bucket key. Auth-off and anonymous requests
    therefore share their configured fallback identity while authenticated users
    cannot consume one another's allowance. The cache bounds per-process state.
    Tool calls and resource reads drain the same bucket.
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

    async def admit[ParamsT, ResultT](
        self,
        context: MiddlewareContext[ParamsT],
        call_next: CallNext[ParamsT, ResultT],
    ) -> ResultT:
        """Charge the bound caller's bucket, then run the wrapped handler."""
        user = await bound_user(request_context(context))
        if user is None:
            raise ToolError("no user resolved before rate limiting")
        if not await self.limiter(user.id).consume():
            raise RateLimitError("caller rate limit exceeded")
        return await call_next(context)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        return await self.admit(context, call_next)

    async def on_read_resource(
        self,
        context: MiddlewareContext[mt.ReadResourceRequestParams],
        call_next: CallNext[mt.ReadResourceRequestParams, ResourceResult],
    ) -> ResourceResult:
        return await self.admit(context, call_next)
