import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

import dbutil
import httpx
import pytest
from fastmcp import settings as fastmcp_settings
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from hypothesis import given
from hypothesis import strategies as st
from pydantic import AnyHttpUrl, SecretStr
from pydantic.types import JsonValue
from tenacity import RetryCallState, Retrying

import aizk.auth as auth_module
from aizk.auth import Auth
from aizk.config import settings
from aizk.integrations import logto as lt
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
    ("accounts", "found"),
    [
        ([{"id": "one", "primaryEmail": "lab@example.com"}], "one"),
        ([{"id": "one", "primaryEmail": "other@example.com"}], None),
        (
            [
                {"id": "one", "primaryEmail": "lab@example.com"},
                {"id": "two", "primaryEmail": "LAB@example.com"},
            ],
            None,
        ),
        ([{"id": "one"}], None),
    ],
)
def test_account_by_email_requires_one_exact_case_insensitive_match(
    monkeypatch: pytest.MonkeyPatch,
    accounts: list[dict[str, str]],
    found: str | None,
) -> None:
    client = lt.LogtoClient()
    response = httpx.Response(
        200,
        json=accounts,
        request=httpx.Request("GET", "https://auth.test/api/users"),
    )
    management = AsyncMock(return_value=response)
    monkeypatch.setattr(client, "management", management)

    account = dbutil.run(client.account_by_email("Lab@example.com"))
    client.caches.invalidate(
        "user-1",
        "user-2",
        organization_ids=("organization-1",),
    )
    dbutil.run(client.close())

    assert (account.id if account is not None else None) == found
    assert management.await_args is not None
    assert management.await_args.kwargs["params"] == {
        "search.primaryEmail": "Lab@example.com",
        "mode.primaryEmail": "exact",
        "page_size": 2,
    }


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

    async def organizations(subject: str, *, fresh: bool = False) -> tuple[lt.Org, ...]:
        return (organization,)

    async def public(*, fresh: bool = False) -> tuple[lt.Org, ...]:
        return (lt.Org(id="public", name="Public", customData={"public": True}),)

    monkeypatch.setattr(client, "user_orgs", organizations)
    monkeypatch.setattr(client, "public_orgs", public)
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
    public_scope = settings.scope_id("public")
    assert user.id == settings.subject_id("user-1")
    assert user.scopes.read == frozenset({user.id, scope, public_scope})
    assert user.scopes.public == frozenset({public_scope})
    assert (scope in user.scopes.write) is writable
    assert public_scope not in user.scopes.write
    assert user.organizations[0].roles == (role,)
    assert user.organizations[0].permissions == permissions
    assert user.organizations[0].writable is writable
    assert user.organizations[1].name == "Public"
    assert user.organizations[1].members == ()


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
        current_organizations = (
            await client.user_orgs("user", fresh=True),
            await client.public_orgs(fresh=True),
        )
        current_account = await client.account("user", fresh=True)
        current_roles = await client.user_roles("user", fresh=True)
        current_members = await client.organization_members("org", fresh=True)
        await client.close()
        return (
            *organizations,
            account,
            roles,
            members,
            *current_organizations,
            current_account,
            current_roles,
            current_members,
        )

    assert dbutil.run(no_logto()) == ((), (), None, (), (), (), (), None, (), ())

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

    async def probe() -> tuple[
        User,
        tuple[lt.Org, ...],
        User,
        User,
        tuple[lt.Role, ...],
        tuple[lt.Role, ...],
    ]:
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
        current = await client.user_subject("user/a", fresh=True)
        current_again = await client.user_subject("user/a", fresh=True)
        organization_roles = await client.organization_roles(fresh=True)
        cached_organization_roles = await client.organization_roles()
        await client.close()
        return (
            user,
            second,
            current,
            current_again,
            organization_roles,
            cached_organization_roles,
        )

    user, second, current, current_again, organization_roles, cached_roles = dbutil.run(probe())

    assert user.name == "Pedro Valois"
    assert user.username == "pedro"
    assert user.roles == ("aizk-user",)
    assert second[0].roles[0].name == "editor"
    assert second[0].scopes[0].name == "write:memory"
    assert second[0].members[0].roles[0].name == "editor"
    assert user.organizations[0].public
    assert user.organizations[0].members[0].username == "pedro"
    assert len(user.organizations) == 1
    assert current == current_again == user
    assert organization_roles[0].name == cached_roles[0].name == "Alpha"
    assert calls.count("/oidc/token") == 1
    assert calls.count("/api/users/user/a/organizations") == 3
    assert calls.count("/api/organizations/org-a/users") == 3


@pytest.mark.parametrize(
    ("account", "roles", "message"),
    [
        (None, (lt.Role(id="user-role", name="aizk-user"),), "no longer exists"),
        (
            lt.Account(id="user-1", isSuspended=True),
            (lt.Role(id="user-role", name="aizk-user"),),
            "suspended",
        ),
        (lt.Account(id="user-1"), (), "application access"),
    ],
    ids=["missing", "suspended", "unassigned"],
)
def test_current_browser_authority_rejects_unusable_accounts(
    monkeypatch: pytest.MonkeyPatch,
    account: lt.Account | None,
    roles: tuple[lt.Role, ...],
    message: str,
) -> None:
    client = lt.LogtoClient()
    monkeypatch.setattr(client, "user_orgs", AsyncMock(return_value=()))
    monkeypatch.setattr(client, "public_orgs", AsyncMock(return_value=()))
    monkeypatch.setattr(client, "account", AsyncMock(return_value=account))
    monkeypatch.setattr(client, "user_roles", AsyncMock(return_value=roles))

    with pytest.raises(lt.LogtoAccessError, match=message):
        dbutil.run(client.user_subject("user-1", fresh=True))

    dbutil.run(client.close())


@pytest.mark.parametrize(("status", "missing"), [(404, True), (503, False)])
def test_current_account_distinguishes_deletion_from_an_outage(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    missing: bool,
) -> None:
    _configure_logto(monkeypatch)
    client = lt.LogtoClient()
    response = httpx.Response(
        status,
        request=httpx.Request("GET", "https://auth.test/api/users/user-1"),
    )
    error = httpx.HTTPStatusError(
        "account lookup failed",
        request=response.request,
        response=response,
    )
    monkeypatch.setattr(client, "management", AsyncMock(side_effect=error))

    if missing:
        assert dbutil.run(client.account("user-1", fresh=True)) is None
    else:
        with pytest.raises(httpx.HTTPStatusError):
            dbutil.run(client.account("user-1", fresh=True))

    dbutil.run(client.close())


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
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
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
    assert not any(path.startswith("/api/organizations/") for path in calls)


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

    async def no_orgs(subject: str, *, fresh: bool = False) -> tuple[lt.Org, ...]:
        return ()

    async def no_public(*, fresh: bool = False) -> tuple[lt.Org, ...]:
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

    async def no_public(*, fresh: bool = False) -> tuple[lt.Org, ...]:
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
    expected = (
        frozenset(settings.scope_id(org.id) for org in organizations) if enabled else frozenset()
    )
    assert user.scopes.public == expected


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
        # Aizk's own scope and resource policy, not FastMCP's internals or wire protocol.
        registration = provider.client_registration_options
        assert registration is not None
        assert registration.default_scopes == ["control", "offline_access", "openid"]
        assert provider.required_scopes == ["control"]
        assert settings.mcp_resource_id == "https://aizk.test/mcp"
        # The committed gateway must proxy exactly the provider's routes plus the MCP paths.
        routes = {route.path for route in provider.get_routes("/mcp")}
        gateway = Path(__file__).parents[2] / "src/deploy/Caddyfile"
        gateway_routes = set(
            next(
                line
                for line in gateway.read_text().splitlines()
                if line.strip().startswith("@mcp path ")
            ).split()[2:]
        )
        assert gateway_routes == routes | {"/mcp", "/mcp/*"}


def test_bearer_uses_the_local_identity_when_auth_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "logto_url", None)
    client = lt.LogtoClient()

    grant = dbutil.run(Auth(client).bearer("ignored"))
    dbutil.run(client.close())

    assert grant is not None
    assert grant.subject == "system"
    assert grant.user.id == settings.default_user_id


@pytest.mark.parametrize(
    ("token", "verified"),
    [
        ("", None),
        ("unverifiable", None),
        ("incomplete", "claimless"),
        ("good", "complete"),
    ],
    ids=["blank", "unverifiable", "invalid-claims", "verified"],
)
def test_bearer_resolves_only_tokens_the_mcp_verifier_would_accept(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
    verified: str | None,
) -> None:
    _configure_logto(monkeypatch)
    client = lt.LogtoClient()
    auth = Auth(client)
    access = (
        None
        if verified is None
        else AccessToken(
            token=token,
            client_id="client",
            scopes=["control"],
            claims={} if verified == "claimless" else _claims(),
        )
    )
    monkeypatch.setattr(auth, "verify_token", AsyncMock(return_value=access))
    monkeypatch.setattr(client, "user_orgs", AsyncMock(return_value=()))
    monkeypatch.setattr(client, "public_orgs", AsyncMock(return_value=()))
    monkeypatch.setattr(client, "account", AsyncMock(return_value=None))
    monkeypatch.setattr(client, "user_roles", AsyncMock(return_value=()))

    grant = dbutil.run(auth.bearer(token))
    dbutil.run(client.close())

    if verified == "complete":
        assert grant is not None
        assert grant.subject == "user-1"
        assert grant.user.id == settings.subject_id("user-1")
    else:
        assert grant is None


def test_verifier_caches_survive_concurrent_auth_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_logto(monkeypatch)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_discovery()))
    client = _mock_client(transport)
    first, second = Auth(client), Auth(client)

    async def probe() -> None:
        ours = await first.get_verifiers()
        theirs = await second.get_verifiers()
        assert await first.get_verifiers() is ours  # the second instance evicted nothing
        assert await second.get_verifiers() is theirs
        await client.close()

    dbutil.run(probe())


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

    async def no_orgs(subject: str, *, fresh: bool = False) -> tuple[lt.Org, ...]:
        return ()

    async def no_public(*, fresh: bool = False) -> tuple[lt.Org, ...]:
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
