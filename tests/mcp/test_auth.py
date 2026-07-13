import asyncio
import time

import dbutil
import httpx
import pytest
from fastmcp.server.auth import AccessToken, RemoteAuthProvider
from pydantic import AnyHttpUrl, SecretStr
from tenacity import RetryCallState, Retrying

import aizk.mcp.auth as auth_module
from aizk.common.auth import logto as lt
from aizk.config import settings
from aizk.mcp.auth import Auth


def claims(subject: str = "user-1") -> dict[str, str | int]:
    now = int(time.time())
    return {
        "iss": "https://auth.test/oidc",
        "sub": subject,
        "aud": "https://aizk.test/mcp",
        "iat": now,
        "exp": now + 60,
    }


def discovery() -> dict[str, str | list[str]]:
    return {
        "issuer": "https://auth.test/oidc",
        "jwks_uri": "https://auth.test/oidc/jwks",
        "token_endpoint": "https://auth.test/oidc/token",
        "id_token_signing_alg_values_supported": ["ES384"],
    }


def configure_logto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "logto_url", AnyHttpUrl("https://auth.test"))
    monkeypatch.setattr(settings, "mcp_public_url", AnyHttpUrl("https://aizk.test"))
    monkeypatch.setattr(settings, "logto_client_id", "client")
    monkeypatch.setattr(settings, "logto_client_secret", SecretStr("secret"))


def mock_client(handler: httpx.MockTransport) -> lt.LogtoClient:
    client = lt.LogtoClient()
    dbutil.run(client.http.aclose())
    client.http = httpx.AsyncClient(transport=handler)
    return client


@pytest.mark.parametrize(
    ("role", "writable"),
    [("editor", True), ("admin", True), ("viewer", False), ("member", False)],
)
def test_client_builds_current_read_and_write_authority(
    monkeypatch: pytest.MonkeyPatch, role: str, writable: bool
) -> None:
    organization = lt.Org(
        id="org-a",
        name="Alpha",
        organizationRoles=[{"id": "role-a", "name": role}],
    )
    client = lt.LogtoClient()

    async def organizations(subject: str) -> tuple[lt.Org, ...]:
        return (organization,)

    monkeypatch.setattr(client, "user_orgs", organizations)
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


def test_client_derives_and_validates_logto_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_logto(monkeypatch)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=discovery()))
    client = mock_client(transport)

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
    assert dbutil.run(client.user_orgs("user")) == ()
    assert dbutil.run(client.public_orgs()) == ()

    configure_logto(monkeypatch)
    invalid = discovery() | {"issuer": "https://other.test/oidc"}
    client = mock_client(httpx.MockTransport(lambda request: httpx.Response(200, json=invalid)))
    with pytest.raises(ValueError, match="different issuer"):
        dbutil.run(client.discovery())
    dbutil.run(client.close())

    def malformed(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=discovery())
        if request.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "m2m"})
        return httpx.Response(200, json=[{}])

    client = mock_client(httpx.MockTransport(malformed))
    assert dbutil.run(client.user_orgs("user")) == ()
    assert dbutil.run(client.public_orgs()) == ()
    dbutil.run(client.close())


def test_client_reads_current_user_roles_and_caches_by_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_logto(monkeypatch)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=discovery())
        if request.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "m2m", "expires_in": 60})
        return httpx.Response(
            200,
            json=[
                {
                    "id": "org-a",
                    "name": "Alpha",
                    "organizationRoles": [{"id": "role-a", "name": "editor"}],
                }
            ],
        )

    client = mock_client(httpx.MockTransport(handler))

    async def probe() -> tuple[tuple[lt.Org, ...], tuple[lt.Org, ...]]:
        first = await client.user_orgs("user/a")
        second = await client.user_orgs("user/a")
        await client.close()
        return first, second

    first, second = dbutil.run(probe())

    assert first is second
    assert first[0].roles[0].name == "editor"
    assert calls == [
        "/oidc/.well-known/openid-configuration",
        "/oidc/token",
        "/api/users/user/a/organizations",
    ]


def test_client_filters_exact_public_flags_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_logto(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=discovery())
        if request.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "m2m"})
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json=[{"id": f"private-{index}", "name": str(index)} for index in range(100)],
            )
        return httpx.Response(
            200,
            json=[
                {"id": "public", "name": "Public", "customData": {"public": True}},
                {"id": "truthy", "name": "Truthy", "customData": {"public": "true"}},
            ],
        )

    client = mock_client(httpx.MockTransport(handler))
    organizations = dbutil.run(client.public_orgs())
    dbutil.run(client.close())

    assert tuple(organization.id for organization in organizations) == ("public",)


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
    configure_logto(monkeypatch)
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
    configure_logto(monkeypatch)
    client = lt.LogtoClient()
    auth = Auth(client)
    access = AccessToken(
        token="verified",
        client_id="client",
        scopes=["control"],
        claims=claims(),
    )

    async def no_orgs(subject: str) -> tuple[lt.Org, ...]:
        return ()

    async def no_public() -> tuple[lt.Org, ...]:
        return ()

    async def duplicate_verification(token: str) -> AccessToken | None:
        raise AssertionError("FastMCP already verified this request")

    monkeypatch.setattr(auth_module, "get_access_token", lambda: access)
    monkeypatch.setattr(client, "user_orgs", no_orgs)
    monkeypatch.setattr(client, "public_orgs", no_public)
    monkeypatch.setattr(auth, "verify_token", duplicate_verification)

    user = dbutil.run(auth.resolve())
    dbutil.run(client.close())

    assert user.id == settings.subject_id("user-1")


@pytest.mark.parametrize(("enabled", "expected"), [(False, "default"), (True, "anonymous")])
def test_resolve_fallback_matches_auth_mode(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, expected: str
) -> None:
    client = lt.LogtoClient()
    auth = Auth(client)
    logto_url = AnyHttpUrl("https://auth.test") if enabled else None
    monkeypatch.setattr(settings, "logto_url", logto_url)
    monkeypatch.setattr(settings, "auth_token", SecretStr(""))
    monkeypatch.setattr(auth_module, "get_access_token", lambda: None)

    async def no_public() -> tuple[lt.Org, ...]:
        return ()

    monkeypatch.setattr(client, "public_orgs", no_public)
    user = dbutil.run(auth.resolve())
    dbutil.run(client.close())
    wanted = settings.anonymous_user_id if expected == "anonymous" else settings.default_user_id
    assert user.id == wanted


def test_resolve_keeps_every_public_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = lt.LogtoClient()
    auth = Auth(client)
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
    assert user.scopes.public == frozenset(settings.scope_id(org.id) for org in organizations)


@pytest.mark.parametrize("enabled", [True, False])
def test_provider_advertises_logto_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    monkeypatch.setattr(
        settings,
        "logto_url",
        AnyHttpUrl("https://auth.test") if enabled else None,
    )
    monkeypatch.setattr(
        settings,
        "mcp_public_url",
        AnyHttpUrl("https://aizk.test") if enabled else None,
    )
    client = lt.LogtoClient()
    provider = Auth(client).provider()
    dbutil.run(client.close())

    assert isinstance(provider, RemoteAuthProvider) is enabled


def test_auth_builds_cached_verifiers_and_resolves_only_valid_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_logto(monkeypatch)
    payload = discovery() | {"id_token_signing_alg_values_supported": ["ES384", "RS256"]}
    client = mock_client(httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))

    class Verifier:
        def __init__(self, *, algorithm: str, **kwargs: object) -> None:
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
                claims=claims(),
            )

    async def no_orgs(subject: str) -> tuple[lt.Org, ...]:
        return ()

    async def no_public() -> tuple[lt.Org, ...]:
        return ()

    monkeypatch.setattr(auth_module, "JWTVerifier", Verifier)
    monkeypatch.setattr(auth_module, "get_access_token", lambda: None)
    monkeypatch.setattr(client, "user_orgs", no_orgs)
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
