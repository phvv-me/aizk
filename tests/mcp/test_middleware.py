from typing import cast

import dbutil
import mcp.types as mt
import mcp_probe
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.resources import ResourceContent, ResourceResult
from fastmcp.server.context import Context
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitError
from fastmcp.tools import ToolResult
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5
from mcp_probe import context_for

import aizk.usage as usage_mod
from aizk.auth import Auth
from aizk.config import settings
from aizk.mcp.middleware import (
    CallerRateLimit,
    IdentityMiddleware,
    bound_user,
    resource_reply_size,
)
from aizk.store import Usage
from aizk.store.identity import User
from aizk.usage import annotate_operation


class FakeContext:
    def __init__(
        self,
        fastmcp_context: Context | None = None,
        message: mt.CallToolRequestParams | mt.ReadResourceRequestParams | None = None,
    ) -> None:
        self.fastmcp_context = fastmcp_context
        self.message = message
        self.method = "tools/call" if isinstance(message, mt.CallToolRequestParams) else None


type ToolContext = MiddlewareContext[mt.CallToolRequestParams]
type ResourceContext = MiddlewareContext[mt.ReadResourceRequestParams]


async def no_account(*args: int | float | None) -> None:
    """Accept one test transport completion without touching PgQueuer."""
    del args


def tool_context(user: User | None = None, name: str = "status") -> ToolContext:
    message = mt.CallToolRequestParams(name=name, arguments={"query": "hello"})
    return cast("ToolContext", FakeContext(context_for(user), message))


def resource_context(user: User | None = None) -> ResourceContext:
    message = mt.ReadResourceRequestParams(uri="aizk://artifacts/1/contents/2")
    return cast("ResourceContext", FakeContext(context_for(user), message))


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

    result = dbutil.run(IdentityMiddleware(auth, no_account).on_call_tool(context, call_next))
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


def test_identity_middleware_binds_the_caller_on_resource_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = User.private(uuid5())
    auth = Auth()

    async def resolve() -> User:
        return resolved

    monkeypatch.setattr(auth, "resolve", resolve)
    context = resource_context()
    users_inside: list[User | None] = []
    expected = ResourceResult(contents=[])

    async def call_next(context: ResourceContext) -> ResourceResult:
        assert context.fastmcp_context is not None
        users_inside.append(await bound_user(context.fastmcp_context))
        return expected

    result = dbutil.run(IdentityMiddleware(auth, no_account).on_read_resource(context, call_next))
    assert result is expected
    assert users_inside == [resolved]


def test_rate_limit_charges_resource_reads_from_the_same_caller_bucket() -> None:
    limit = CallerRateLimit(max_requests_per_second=0.2)
    user = User.private(uuid5())
    expected = ResourceResult(contents=[])

    async def call_next(context: ResourceContext) -> ResourceResult:
        return expected

    assert dbutil.run(limit.on_read_resource(resource_context(user), call_next)) is expected
    with pytest.raises(RateLimitError, match="caller rate limit"):
        dbutil.run(limit.on_read_resource(resource_context(user), call_next))
    with pytest.raises(ToolError, match="no user resolved"):
        dbutil.run(limit.on_read_resource(resource_context(), call_next))


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


def test_identity_middleware_accounts_a_recall_tool_call_on_the_serving_span() -> None:
    user = User.private(uuid5())
    middleware = mcp_probe.transport_middleware(user)
    context = tool_context(name="recall")
    reply = ToolResult(content=[mt.TextContent(type="text", text="evidence")])

    async def call_next(context: ToolContext) -> ToolResult:
        del context
        annotate_operation(Usage.Event.Operation.recall)  # as the memory service stamps it
        return reply

    result = dbutil.run(
        mcp_probe.through_transport(lambda: middleware.on_call_tool(context, call_next))
    )

    assert result is reply
    capture = mcp_probe.captured.pop()
    assert not mcp_probe.captured
    assert capture.operation is Usage.Event.Operation.recall
    assert capture.user_id == user.id
    assert capture.targets == (user.id,)
    assert context.message is not None
    declared = context.message.model_dump_json(exclude_none=True).encode()
    assert capture.request_bytes == len(declared)
    assert capture.response_bytes == sum(
        len(block.model_dump_json().encode()) for block in reply.content
    )
    assert capture.duration_ms >= 0


def test_unaccounted_tools_and_failed_calls_leave_no_capture() -> None:
    user = User.private(uuid5())
    middleware = mcp_probe.transport_middleware(user)

    async def call_next(context: ToolContext) -> ToolResult:
        del context
        return ToolResult(content=[])

    async def failing(context: ToolContext) -> ToolResult:
        del context
        annotate_operation(Usage.Event.Operation.recall)  # stamped before the failure
        raise ToolError("broken")

    dbutil.run(
        mcp_probe.through_transport(
            lambda: middleware.on_call_tool(tool_context(name="status"), call_next)
        )
    )
    with pytest.raises(ToolError, match="broken"):
        dbutil.run(
            mcp_probe.through_transport(
                lambda: middleware.on_call_tool(tool_context(name="recall"), failing)
            )
        )
    assert not mcp_probe.captured


def test_resource_reply_size_counts_binary_and_text_contents() -> None:
    reply = ResourceResult(
        contents=[ResourceContent(b"ab"), ResourceContent("text")],
    )
    assert resource_reply_size(reply) == len(b"ab") + len(b"text")


def test_detached_session_dispatch_still_accounts_through_its_own_root_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User.private(uuid5())
    middleware = mcp_probe.transport_middleware(user)
    monkeypatch.setattr(
        usage_mod.trace, "get_tracer", lambda name: mcp_probe.provider.get_tracer(name)
    )
    reply = ToolResult(content=[])

    async def call_next(context: ToolContext) -> ToolResult:
        del context
        annotate_operation(Usage.Event.Operation.share)
        return reply

    result = dbutil.run(middleware.on_call_tool(tool_context(name="share"), call_next))

    assert result is reply
    capture = mcp_probe.captured.pop()
    assert not mcp_probe.captured
    assert capture.operation is Usage.Event.Operation.share
    assert capture.user_id == user.id
