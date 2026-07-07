import uuid

import dbutil
import pytest
from fastmcp.exceptions import ToolError

import aizk.mcp.principal as principal_mod
from aizk.config import settings
from aizk.mcp.principal import (
    AUTH_TOKEN_ENV,
    User,
    bearer_token,
    current_user,
    require_identified,
    resolve_user,
)

pytestmark = pytest.mark.usefixtures("migrated_db")


def a_principal() -> User:
    """A resolved caller identity for the pure gate tests."""
    return User(id=uuid.uuid4())


def test_bearer_token_prefers_the_environment_over_the_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stdio `AIZK_AUTH_TOKEN` env var wins; otherwise the HTTP Authorization bearer scheme."""
    monkeypatch.setattr(
        principal_mod, "get_http_headers", lambda: {"authorization": "Bearer hval"}
    )
    monkeypatch.setenv(AUTH_TOKEN_ENV, "envval")
    assert bearer_token() == "envval"
    monkeypatch.delenv(AUTH_TOKEN_ENV, raising=False)
    assert bearer_token() == "hval"


@pytest.mark.parametrize(
    ("header", "expected"),
    [("Bearer tok", "tok"), ("Basic tok", None), ("", None), ("bearer ", None)],
)
def test_bearer_token_reads_only_the_bearer_scheme(
    monkeypatch: pytest.MonkeyPatch, header: str, expected: str | None
) -> None:
    """Only a non-empty `Bearer <token>` header resolves; any other scheme drops through."""
    monkeypatch.delenv(AUTH_TOKEN_ENV, raising=False)
    monkeypatch.setattr(principal_mod, "get_http_headers", lambda: {"authorization": header})
    assert bearer_token() == expected


def test_require_identified_refuses_the_anonymous_principal() -> None:
    """The anonymous id is read-only; any other principal passes the write gate."""
    named = a_principal()
    assert require_identified(named) is named
    with pytest.raises(ToolError, match="anonymous"):
        require_identified(User(id=settings.anonymous_user_id))


def test_current_principal_reads_the_resolved_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """`current_user` returns the User the middleware stashed, else a loud ToolError."""
    resolved = a_principal()

    class Ctx:
        def __init__(self, state: object) -> None:
            self._state = state

        def get_state(self, key: str) -> object:
            return self._state

    monkeypatch.setattr(principal_mod, "get_context", lambda: Ctx(resolved))
    assert current_user() == resolved
    monkeypatch.setattr(principal_mod, "get_context", lambda: Ctx(None))
    with pytest.raises(ToolError, match="no principal"):
        current_user()


@pytest.mark.parametrize("http", [False, True])
def test_resolve_principal_falls_back_by_transport(
    monkeypatch: pytest.MonkeyPatch, http: bool
) -> None:
    """With no token, stdio resolves the configured principal and HTTP the anonymous one."""
    monkeypatch.setattr(principal_mod, "bearer_token", lambda: None)
    monkeypatch.setattr(settings, "mcp_http", http)

    async def body() -> None:
        await dbutil.reset_db()
        await dbutil.seed_user(settings.default_user_id, is_admin=True)
        principal = await resolve_user()
        expected = settings.anonymous_user_id if http else settings.default_user_id
        assert principal.id == expected

    dbutil.run(body())


def test_resolve_principal_uses_a_verified_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolvable bearer token maps straight to its principal, past the transport fallback."""
    token_principal = uuid.uuid4()

    async def from_token(token: str) -> uuid.UUID:
        return token_principal

    monkeypatch.setattr(principal_mod, "bearer_token", lambda: "a-token")
    monkeypatch.setattr(
        principal_mod.UserRow, "from_token", classmethod(lambda cls, token: from_token(token))
    )

    async def body() -> None:
        await dbutil.reset_db()
        await dbutil.seed_user(token_principal)
        principal = await resolve_user()
        assert principal.id == token_principal

    dbutil.run(body())
