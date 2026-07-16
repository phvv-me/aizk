import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

import dbutil
import httpx
import pytest
from fastmcp import FastMCP
from fastmcp import settings as fastmcp_settings
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from hypothesis import given
from hypothesis import strategies as st
from pydantic import AnyHttpUrl, SecretStr
from pydantic.types import JsonValue
from starlette.testclient import TestClient
from tenacity import RetryCallState, Retrying

import aizk.mcp.auth as auth_module
from aizk.common.auth import logto as lt
from aizk.config import settings
from aizk.mcp.auth import Auth
from aizk.store.identity import User


def _claims(subject: str = "user-1") -> dict[str, str | int]:
    now = int(time.time())
    return {
        "iss": "https://auth.test/oidc",
        "sub": subject,
        "aud": "https://aizk.test/mcp",
        "iat": now,
        "exp": now + 60,
    }


def _discovery() -> dict[str, str | list[str]]:
    return {
        "issuer": "https://auth.test/oidc",
        "authorization_endpoint": "https://auth.test/oidc/auth",
        "jwks_uri": "https://auth.test/oidc/jwks",
        "token_endpoint": "https://auth.test/oidc/token",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["ES384"],
    }


def _configure_logto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "logto_url", AnyHttpUrl("https://auth.test"))
    monkeypatch.setattr(settings, "mcp_public_url", AnyHttpUrl("https://aizk.test"))
    monkeypatch.setattr(settings, "logto_client_id", "client")
    monkeypatch.setattr(settings, "logto_client_secret", SecretStr("secret"))
    monkeypatch.setattr(settings, "oauth_client_id", "oauth-client")
    monkeypatch.setattr(settings, "oauth_client_secret", SecretStr("oauth-secret"))


def _mock_client(handler: httpx.MockTransport) -> lt.LogtoClient:
    client = lt.LogtoClient()
    dbutil.run(client.http.aclose())
    client.http = httpx.AsyncClient(transport=handler)
    return client


@pytest.mark.parametrize(
    ("role", "permissions", "writable"),
    [
        ("custom", ("write:memory",), True),
        ("admin", (), False),
        ("editor", ("read",), False),
        ("viewer", (), False),
    ],
)
def test_client_builds_current_read_and_write_authority(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    permissions: tuple[str, ...],
    writable: bool,
) -> None:
    organization = lt.Org(
        id="org-a",
        name="Alpha",
        organizationRoles=[{"id": "role-a", "name": role}],
        scopes=[{"id": name, "name": name} for name in permissions],
    )
    client = lt.LogtoClient()

    async def organizations(subject: str) -> tuple[lt.Org, ...]:
        return (organization,)

    monkeypatch.setattr(client, "user_orgs", organizations)
    monkeypatch.setattr(client, "account", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "user_roles", AsyncMock(return_value=()))
    now = int(time.time())
    user = dbutil.run(
        client.user(
            lt.Claims(
                iss=AnyHttpUrl("https://auth.test/oidc"),
                sub="user-1",
                aud="https://aizk.test/mcp",
                iat=now,
                exp=now + 60,
            )
        )
    )
    dbutil.run(client.close())

    scope = settings.scope_id("org-a")
    assert user.id == settings.subject_id("user-1")
    assert user.scopes.read == frozenset({user.id, scope})
    assert (scope in user.scopes.write) is writable
    assert user.organizations[0].roles == (role,)
    assert user.organizations[0].permissions == permissions
    assert user.organizations[0].writable is writable


def test_client_derives_and_validates_logto_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_logto(monkeypatch)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_discovery()))
    client = _mock_client(transport)

    result = dbutil.run(client.discovery())
    dbutil.run(client.close())

    assert str(client.issuer) == "https://auth.test/oidc"
    assert str(result.jwks_uri) == "https://auth.test/oidc/jwks"
    assert result.signing_algorithms == ("ES384",)


def test_client_rejects_untrusted_discovery_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = lt.LogtoClient()
    monkeypatch.setattr(settings, "logto_url", None)
    with pytest.raises(RuntimeError, match="tenant endpoint"):
        _ = client.issuer
    with pytest.raises(RuntimeError, match="tenant endpoint"):
        _ = client.management_url

    async def no_logto() -> tuple[
        tuple[lt.Org, ...],
        tuple[lt.Org, ...],
        lt.Account | None,
        tuple[lt.Role, ...],
        tuple[lt.Member, ...],
    ]:
        organizations = await client.user_orgs("user"), await client.public_orgs()
        account = await client.account("user")
        roles = await client.user_roles("user")
        members = await client.organization_members("org")
        await client.close()
        return *organizations, account, roles, members

    assert dbutil.run(no_logto()) == ((), (), None, (), ())

    _configure_logto(monkeypatch)
    invalid = _discovery() | {"issuer": "https://other.test/oidc"}
    client = _mock_client(httpx.MockTransport(lambda request: httpx.Response(200, json=invalid)))
    with pytest.raises(ValueError, match="different issuer"):
        dbutil.run(client.discovery())
    dbutil.run(client.close())

    def malformed(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_discovery())
        if request.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "m2m"})
        return httpx.Response(200, json=[{}])

    client = _mock_client(httpx.MockTransport(malformed))

    async def malformed_authority() -> tuple[
        tuple[lt.Org, ...],
        tuple[lt.Org, ...],
        lt.Account | None,
        tuple[lt.Role, ...],
        tuple[lt.Member, ...],
        tuple[lt.OrganizationScope, ...],
    ]:
        organizations = await client.user_orgs("user"), await client.public_orgs()
        account = await client.account("user")
        roles = await client.user_roles("user")
        members = await client.organization_members("org")
        scopes = await client.user_scopes("user", "org")
        await client.close()
        return *organizations, account, roles, members, scopes

    assert dbutil.run(malformed_authority()) == ((), (), None, (), (), ())


def test_client_reads_current_user_directory_and_caches_by_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_logto(monkeypatch)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_discovery())
        if request.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "m2m", "expires_in": 60})
        if request.url.path == "/api/users/user/a":
            return httpx.Response(
                200,
                json={
                    "id": "user/a",
                    "username": "pedro",
                    "name": "Pedro Valois",
                    "avatar": "https://images.test/pedro.png",
                },
            )
        if request.url.path == "/api/users/user/a/roles":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "role-user",
                        "name": "aizk-user",
                        "description": "AIZK user",
                    }
                ],
            )
        if request.url.path.endswith("/scopes"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "write-memory",
                        "name": "write:memory",
                        "description": "Write shared memory",
                    }
                ],
            )
        if request.url.path == "/api/organizations/org-a/users":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "user/a",
                        "username": "pedro",
                        "name": "Pedro Valois",
                        "organizationRoles": [{"id": "role-a", "name": "editor"}],
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "id": "org-a",
                    "name": "Alpha",
                    "description": "Shared research",
                    "customData": {"public": True},
                    "organizationRoles": [{"id": "role-a", "name": "editor"}],
                }
            ],
        )

    client = _mock_client(httpx.MockTransport(handler))

    async def probe() -> tuple[User, tuple[lt.Org, ...]]:
        now = int(time.time())
        user = await client.user(
            lt.Claims(
                iss=AnyHttpUrl("https://auth.test/oidc"),
                sub="user/a",
                aud="https://aizk.test/mcp",
                iat=now,
                exp=now + 60,
            )
        )
        second = await client.user_orgs("user/a")
        await client.close()
        return user, second

    user, second = dbutil.run(probe())

    assert user.name == "Pedro Valois"
    assert user.username == "pedro"
    assert user.roles == ("aizk-user",)
    assert second[0].roles[0].name == "editor"
    assert second[0].scopes[0].name == "write:memory"
    assert second[0].members[0].roles[0].name == "editor"
    assert calls.count("/oidc/token") == 1
    assert calls.count("/api/users/user/a/organizations") == 1


@given(
    candidate_flag=st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text()
)
def test_client_filters_exact_public_flags_across_pages(
    monkeypatch: pytest.MonkeyPatch,
    candidate_flag: JsonValue,
) -> None:
    _configure_logto(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_discovery())
        if request.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "m2m"})
        if request.url.path.startswith("/api/organizations/"):
            return httpx.Response(200, json=[])
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json=[{"id": f"private-{index}", "name": str(index)} for index in range(100)],
            )
        return httpx.Response(
            200,
            json=[
                {"id": "public", "name": "Public", "customData": {"public": True}},
                {
                    "id": "candidate",
                    "name": "Candidate",
                    "customData": {"public": candidate_flag},
                },
            ],
        )

    client = _mock_client(httpx.MockTransport(handler))
    organizations = dbutil.run(client.public_orgs())
    dbutil.run(client.close())

    assert tuple(organization.id for organization in organizations) == (
        ("public", "candidate") if candidate_flag is True else ("public",)
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ValueError("not HTTP"), False),
        (httpx.ConnectError("down", request=httpx.Request("GET", "https://x")), True),
        (
            httpx.ConnectError(
                "down",
                request=httpx.Request("GET", "https://x", extensions={"retryable": False}),
            ),
            False,
        ),
        (
            httpx.HTTPStatusError(
                "busy",
                request=httpx.Request("GET", "https://x"),
                response=httpx.Response(503),
            ),
            True,
        ),
        (
            httpx.HTTPStatusError(
                "bad",
                request=httpx.Request("GET", "https://x"),
                response=httpx.Response(400),
            ),
            False,
        ),
    ],
)
def test_client_retries_only_safe_transient_http_failures(
    error: BaseException, expected: bool
) -> None:
    assert lt.LogtoClient._retryable(error) is expected
    state = RetryCallState(Retrying(), None, (), {})
    lt.LogtoClient._log_retry(state)
    state.set_exception((type(error), error, error.__traceback__))
    lt.LogtoClient._log_retry(state)


def test_client_token_cache_coalesces_waiters(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_logto(monkeypatch)
    client = lt.LogtoClient()

    async def probe() -> None:
        await client._token_lock.acquire()
        waiter = asyncio.create_task(client._access_token())
        await asyncio.sleep(0)
        client._token = lt.Token(access_token="cached")
        client._token_expires_at = float("inf")
        client._token_lock.release()
        assert await waiter == "cached"
        assert await client._access_token() == "cached"
        await client.close()

    dbutil.run(probe())


def test_resolve_reuses_the_access_token_fastmcp_already_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_logto(monkeypatch)
    client = lt.LogtoClient()
    auth = Auth(client)
    access = AccessToken(
        token="verified",
        client_id="client",
        scopes=["control"],
        claims=_claims(),
    )

    async def no_orgs(subject: str) -> tuple[lt.Org, ...]:
        return ()

    async def no_public() -> tuple[lt.Org, ...]:
        return ()

    async def duplicate_verification(token: str) -> AccessToken | None:
        raise AssertionError("FastMCP already verified this request")

    monkeypatch.setattr(auth_module, "get_access_token", lambda: access)
    monkeypatch.setattr(client, "user_orgs", no_orgs)
    monkeypatch.setattr(client, "account", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "user_roles", AsyncMock(return_value=()))
    monkeypatch.setattr(client, "public_orgs", no_public)
    monkeypatch.setattr(auth, "verify_token", duplicate_verification)

    user = dbutil.run(auth.resolve())
    dbutil.run(client.close())

    assert user.id == settings.subject_id("user-1")


@given(missing=st.sets(st.sampled_from(tuple(_claims())), min_size=1))
def test_resolve_fails_closed_when_verified_claims_are_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    missing: set[str],
) -> None:
    _configure_logto(monkeypatch)
    payload = _claims()
    for name in missing:
        payload.pop(name)
    access = AccessToken(
        token="verified",
        client_id="client",
        scopes=["control"],
        claims=payload,
    )
    client = lt.LogtoClient()

    async def no_public() -> tuple[lt.Org, ...]:
        return ()

    monkeypatch.setattr(auth_module, "get_access_token", lambda: access)
    monkeypatch.setattr(client, "public_orgs", no_public)

    user = dbutil.run(Auth(client).resolve())
    dbutil.run(client.close())

    assert user.id == settings.anonymous_user_id


@pytest.mark.parametrize("enabled", [False, True], ids=["local", "public"])
def test_resolve_fallback_matches_auth_mode_and_keeps_public_scopes(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    client = lt.LogtoClient()
    auth = Auth(client)
    logto_url = AnyHttpUrl("https://auth.test") if enabled else None
    monkeypatch.setattr(settings, "logto_url", logto_url)
    monkeypatch.setattr(settings, "auth_token", SecretStr(""))
    monkeypatch.setattr(auth_module, "get_access_token", lambda: None)

    organizations = (
        lt.Org(id="one", name="Same", customData={"public": True}),
        lt.Org(id="two", name="Same", customData={"public": True}),
    )

    async def public() -> tuple[lt.Org, ...]:
        return organizations

    monkeypatch.setattr(client, "public_orgs", public)
    user = dbutil.run(auth.resolve())
    dbutil.run(client.close())
    wanted = settings.anonymous_user_id if enabled else settings.default_user_id
    assert user.id == wanted
    assert user.scopes.public == frozenset(settings.scope_id(org.id) for org in organizations)


@pytest.mark.parametrize("enabled", [True, False])
def test_provider_advertises_logto_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enabled: bool
) -> None:
    if enabled:
        _configure_logto(monkeypatch)
    else:
        monkeypatch.setattr(settings, "logto_url", None)
        monkeypatch.setattr(settings, "mcp_public_url", None)
    monkeypatch.setattr(fastmcp_settings, "home", tmp_path)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kwargs: httpx.Response(
            200,
            json=_discovery(),
            request=httpx.Request("GET", str(url)),
        ),
    )
    client = lt.LogtoClient()
    provider = Auth(client).provider()
    dbutil.run(client.close())

    assert isinstance(provider, OIDCProxy) is enabled
    if isinstance(provider, OIDCProxy):
        registration = provider.client_registration_options
        assert registration is not None
        assert registration.default_scopes == ["control", "offline_access", "openid"]
        assert provider.required_scopes == ["control"]
        assert provider._fastmcp_access_token_expiry_seconds == 31_536_000
        assert provider._token_expiry_threshold_seconds == 60
        assert provider._extra_authorize_params == {"prompt": "consent"}
        assert provider._extra_token_params == {"resource": "https://aizk.test/mcp"}
        routes = {route.path for route in provider.get_routes("/mcp")}
        assert {
            "/.well-known/oauth-authorization-server",
            "/auth/callback",
            "/authorize",
            "/register",
            "/token",
        } <= routes
        app = FastMCP("oauth-probe", auth=provider).http_app(path="/mcp")
        with TestClient(app) as browser:
            metadata = browser.get("/.well-known/oauth-authorization-server").json()
            registration = browser.post(
                "/register",
                json={
                    "client_name": "MCP client",
                    "redirect_uris": ["http://127.0.0.1:8912/callback/session"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                    "scope": "control offline_access openid",
                },
            )
        assert metadata["registration_endpoint"] == "https://aizk.test/register"
        assert metadata["scopes_supported"] == ["control", "offline_access", "openid"]
        assert registration.status_code == 201
        assert registration.json()["redirect_uris"] == ["http://127.0.0.1:8912/callback/session"]


def test_auth_builds_cached_verifiers_and_resolves_only_valid_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_logto(monkeypatch)
    payload = _discovery() | {"id_token_signing_alg_values_supported": ["ES384", "RS256"]}
    client = _mock_client(httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))

    class Verifier:
        def __init__(
            self,
            *,
            jwks_uri: str,
            issuer: str,
            algorithm: str,
            required_scopes: list[str],
            audience: str,
            http_client: httpx.AsyncClient,
        ) -> None:
            del jwks_uri, issuer, required_scopes, audience, http_client
            self.algorithm = algorithm

        async def verify_token(self, token: str) -> AccessToken | None:
            if token == "invalid":
                return AccessToken(token=token, client_id="client", scopes=[], claims={})
            if token != self.algorithm:
                return None
            return AccessToken(
                token=token,
                client_id="client",
                scopes=["control"],
                claims=_claims(),
            )

    async def no_orgs(subject: str) -> tuple[lt.Org, ...]:
        return ()

    async def no_public() -> tuple[lt.Org, ...]:
        return ()

    monkeypatch.setattr(auth_module, "JWTVerifier", Verifier)
    monkeypatch.setattr(auth_module, "get_access_token", lambda: None)
    monkeypatch.setattr(client, "user_orgs", no_orgs)
    monkeypatch.setattr(client, "account", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "user_roles", AsyncMock(return_value=()))
    monkeypatch.setattr(client, "public_orgs", no_public)
    auth = Auth(client)

    async def probe() -> None:
        verifiers = await auth.get_verifiers()
        assert verifiers is await auth.get_verifiers()
        assert [verifier.algorithm for verifier in verifiers] == ["ES384", "RS256"]
        assert await auth.verify_token("missing") is None
        monkeypatch.setattr(settings, "auth_token", SecretStr("RS256"))
        assert (await auth.resolve()).id == settings.subject_id("user-1")
        monkeypatch.setattr(settings, "auth_token", SecretStr("invalid"))
        assert (await auth.resolve()).is_anonymous()
        monkeypatch.setattr(settings, "logto_url", None)
        assert await Auth(client).verify_token("ES384") is None
        await client.close()

    dbutil.run(probe())
