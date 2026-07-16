from typing import cast

import dbutil
import mcp.types as mt
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitError
from fastmcp.tools import ToolResult
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5
from mcp_probe import context_for

from aizk.config import settings
from aizk.mcp.auth import Auth
from aizk.mcp.middleware import CallerRateLimit, IdentityMiddleware, bound_user
from aizk.store.identity import User


class FakeContext:
    def __init__(self, fastmcp_context: Context | None = None) -> None:
        self.fastmcp_context = fastmcp_context


type ToolContext = MiddlewareContext[mt.CallToolRequestParams]


def tool_context(user: User | None = None) -> ToolContext:
    return cast("ToolContext", FakeContext(context_for(user)))


@given(rate=st.floats(min_value=0.01, max_value=1000))
def test_anonymous_bucket_is_sized_to_a_five_second_burst_never_below_one(rate: float) -> None:
    limiter = CallerRateLimit(max_requests_per_second=rate).limiter(uuid5())
    assert limiter.capacity == max(1, round(rate * 5))
    assert limiter.refill_rate == rate


def test_user_middleware_resolves_once_and_stashes_the_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = uuid5()
    user_id = uuid5()
    resolved = User.authorized(
        user_id,
        read=(user_id, org),
        write=(user_id, org),
    )

    auth = Auth()

    async def resolve() -> User:
        return resolved

    monkeypatch.setattr(auth, "resolve", resolve)
    context = tool_context()
    reached: list[ToolContext] = []
    users_inside: list[User | None] = []
    expected = ToolResult(content=[])

    async def call_next(context: ToolContext) -> ToolResult:
        reached.append(context)
        assert context.fastmcp_context is not None
        users_inside.append(await bound_user(context.fastmcp_context))
        return expected

    result = dbutil.run(IdentityMiddleware(auth).on_call_tool(context, call_next))
    assert result is expected
    assert reached == [context]  # the wrapped handler ran once, after the stash
    assert users_inside == [resolved]


def test_middleware_refuses_a_call_without_a_request_context() -> None:
    async def call_next(context: ToolContext) -> ToolResult:
        raise AssertionError("the wrapped handler must never run")

    bare = cast("ToolContext", FakeContext())
    with pytest.raises(ToolError, match="no request context"):
        dbutil.run(CallerRateLimit(max_requests_per_second=1.0).on_call_tool(bare, call_next))


def test_rate_limit_refuses_a_context_without_a_resolved_user() -> None:
    async def call_next(context: ToolContext) -> ToolResult:
        raise AssertionError("the wrapped handler must never run")

    with pytest.raises(ToolError, match="no user resolved"):
        dbutil.run(
            CallerRateLimit(max_requests_per_second=1.0).on_call_tool(tool_context(), call_next)
        )


def test_bound_user_ignores_a_foreign_state_value() -> None:
    async def body() -> User | None:
        request = context_for()
        await request.set_state("aizk_user", "not a user", serializable=False)
        return await bound_user(request)

    assert dbutil.run(body()) is None


def test_rate_limit_isolates_authenticated_callers() -> None:
    limit = CallerRateLimit(max_requests_per_second=0.2)
    first = tool_context(User.private(uuid5()))
    second = tool_context(User.private(uuid5()))
    expected = ToolResult(content=[])

    async def call_next(context: ToolContext) -> ToolResult:
        return expected

    assert dbutil.run(limit.on_call_tool(first, call_next)) is expected
    with pytest.raises(RateLimitError, match="caller rate limit"):
        dbutil.run(limit.on_call_tool(first, call_next))
    assert dbutil.run(limit.on_call_tool(second, call_next)) is expected


def test_rate_limit_forgets_the_least_recently_used_caller_after_4096_buckets() -> None:
    limit = CallerRateLimit(max_requests_per_second=1.0)
    first_id = uuid5()
    first = limit.limiter(first_id)

    for _ in range(4096):
        limit.limiter(uuid5())

    assert limit.limiter(first_id) is not first


def test_rate_limit_drains_one_shared_burst_then_refuses_the_stranger() -> None:
    limit = CallerRateLimit(max_requests_per_second=0.2)
    context = tool_context(User.private(settings.anonymous_user_id))
    served: list[ToolContext] = []
    expected = ToolResult(content=[])

    async def call_next(context: ToolContext) -> ToolResult:
        served.append(context)
        return expected

    assert dbutil.run(limit.on_call_tool(context, call_next)) is expected
    with pytest.raises(RateLimitError, match="caller rate limit"):
        dbutil.run(limit.on_call_tool(context, call_next))
    assert served == [context]  # only the admitted call ever reached the wrapped handler
