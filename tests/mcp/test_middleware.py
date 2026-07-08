import uuid

import dbutil
import pytest
from fastmcp.server.middleware.rate_limiting import RateLimitError
from hypothesis import given
from hypothesis import strategies as st

import aizk.mcp.middleware as middleware_module
from aizk.config import settings
from aizk.mcp.middleware import AnonymousRateLimit, IdentityMiddleware
from aizk.mcp.user import USER_STATE_KEY, User


class FakeFastmcpContext:
    """A request context stand-in exposing the state slot the middleware reads and writes.

    state: the initial Context state, the resolved `User` a downstream middleware reads back.
    """

    def __init__(self, state: dict[str, object] | None = None) -> None:
        self.state = state or {}

    def set_state(self, key: str, value: object) -> None:
        self.state[key] = value

    def get_state(self, key: str) -> object:
        return self.state.get(key)


class FakeContext:
    """A `MiddlewareContext` stand-in carrying only the `fastmcp_context` the middleware reads."""

    def __init__(self, state: dict[str, object] | None = None) -> None:
        self.fastmcp_context = FakeFastmcpContext(state)


@given(rate=st.floats(min_value=0.01, max_value=1000))
def test_anonymous_bucket_is_sized_to_a_five_second_burst_never_below_one(rate: float) -> None:
    """The bucket holds a five-second burst of the sustained rate, floored at one token."""
    limiter = AnonymousRateLimit(max_requests_per_second=rate).limiter
    assert limiter.capacity == max(1, round(rate * 5))
    assert limiter.refill_rate == rate


def test_user_middleware_resolves_once_and_stashes_it_for_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`on_call_tool` resolves the caller once and stashes it in Context state, then delegates."""
    resolved = User(id=uuid.uuid4())
    monkeypatch.setattr(middleware_module, "resolve_user", lambda: _async_return(resolved))
    context = FakeContext()
    reached: list[object] = []

    async def call_next(ctx: object) -> str:
        reached.append(ctx)
        return "ok"

    result = dbutil.run(IdentityMiddleware().on_call_tool(context, call_next))
    assert result == "ok"
    assert reached == [context]  # the wrapped handler ran once, after the stash
    assert context.fastmcp_context.get_state(USER_STATE_KEY) == resolved


@pytest.mark.parametrize(
    "state",
    [None, User(id=uuid.uuid4())],
    ids=["unresolved", "authenticated"],
)
def test_rate_limit_lets_any_non_anonymous_call_pass_uncharged(state: object) -> None:
    """A missing or authenticated user is never charged, so the shared bucket stays full."""
    limit = AnonymousRateLimit(max_requests_per_second=0.2)  # capacity == max(1, round(1.0)) == 1
    seed = {USER_STATE_KEY: state} if state is not None else {}
    context = FakeContext(seed)

    async def call_next(ctx: object) -> str:
        return "ok"

    for _ in range(3):  # more calls than the lone burst token, yet none consume it
        assert dbutil.run(limit.on_call_tool(context, call_next)) == "ok"


def test_rate_limit_drains_one_shared_burst_then_refuses_the_stranger() -> None:
    """The anonymous user drains the lone burst token, then the next call is refused."""
    limit = AnonymousRateLimit(max_requests_per_second=0.2)  # capacity floors at one token
    anon = User(id=settings.anonymous_user_id)
    context = FakeContext({USER_STATE_KEY: anon})
    served: list[object] = []

    async def call_next(ctx: object) -> str:
        served.append(ctx)
        return "ok"

    assert dbutil.run(limit.on_call_tool(context, call_next)) == "ok"  # the lone burst token
    with pytest.raises(RateLimitError, match="anonymous rate limit"):
        dbutil.run(limit.on_call_tool(context, call_next))  # bucket drained, stranger refused
    assert served == [context]  # only the admitted call ever reached the wrapped handler


async def _async_return[T](value: T) -> T:
    """Await to `value`, the coroutine a patched zero-arg `resolve_user` returns."""
    return value
