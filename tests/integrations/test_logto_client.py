import dbutil
import httpx
import pytest
from pydantic import AnyHttpUrl, SecretStr

from aizk.config import settings
from aizk.integrations import logto as lt


def configure_logto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "logto_url", AnyHttpUrl("https://auth.test"))
    monkeypatch.setattr(settings, "mcp_public_url", AnyHttpUrl("https://aizk.test"))
    monkeypatch.setattr(settings, "logto_client_id", "client")
    monkeypatch.setattr(settings, "logto_client_secret", SecretStr("secret"))


def discovery_payload() -> dict[str, str | list[str]]:
    return {
        "issuer": "https://auth.test/oidc",
        "authorization_endpoint": "https://auth.test/oidc/auth",
        "jwks_uri": "https://auth.test/oidc/jwks",
        "token_endpoint": "https://auth.test/oidc/token",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["ES384"],
    }


def mock_client(handler: httpx.MockTransport) -> lt.LogtoClient:
    client = lt.LogtoClient()
    dbutil.run(client.http.aclose())
    client.http = httpx.AsyncClient(transport=handler)
    return client


def request(method: str, retryable: bool | None = None) -> httpx.Request:
    extensions = {} if retryable is None else {"retryable": retryable}
    return httpx.Request(method, "https://auth.test/api/things", extensions=extensions)


@pytest.mark.parametrize(
    ("method", "retryable", "expected"),
    [
        ("POST", None, False),
        ("PATCH", None, False),
        ("POST", True, True),
        ("GET", None, True),
        ("DELETE", None, True),
        ("PUT", None, True),
        ("GET", False, False),
    ],
)
def test_retries_default_to_idempotent_methods_with_explicit_opt_in(
    method: str, retryable: bool | None, expected: bool
) -> None:
    timeout = httpx.ReadTimeout("slow", request=request(method, retryable))
    assert lt.LogtoClient._retryable(timeout) is expected


def test_token_request_opts_back_into_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_logto(monkeypatch)
    token_extensions: list[bool | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=discovery_payload())
        assert req.url.path == "/oidc/token"
        token_extensions.append(req.extensions.get("retryable"))
        return httpx.Response(200, json={"access_token": "m2m", "expires_in": 60})

    client = mock_client(httpx.MockTransport(handler))

    token = dbutil.run(client._access_token())
    dbutil.run(client.close())

    assert token == "m2m"
    assert token_extensions == [True]


def test_invalidate_all_evicts_every_cached_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_logto(monkeypatch)
    reads: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=discovery_payload())
        if req.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "m2m", "expires_in": 60})
        reads.append(req.url.path)
        return httpx.Response(200, json=[{"id": "role-a", "name": "admin"}])

    client = mock_client(httpx.MockTransport(handler))

    async def probe() -> None:
        await client.organizations()
        await client.organizations()
        await client.organization_roles()
        client.caches.invalidate_all()
        await client.organizations()
        await client.organization_roles()
        await client.close()

    dbutil.run(probe())

    assert reads == [
        "/api/organizations",
        "/api/organization-roles",
        "/api/organizations",
        "/api/organization-roles",
    ]


def test_fresh_authority_screens_a_deleted_account_before_dependent_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_logto(monkeypatch)
    dependents: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=discovery_payload())
        if req.url.path == "/oidc/token":
            return httpx.Response(200, json={"access_token": "m2m", "expires_in": 60})
        if req.url.path == "/api/users/gone":
            return httpx.Response(404)
        dependents.append(req.url.path)
        return httpx.Response(404)

    client = mock_client(httpx.MockTransport(handler))

    with pytest.raises(lt.LogtoAccessError, match="no longer exists"):
        dbutil.run(client.user_subject("gone", fresh=True))
    dbutil.run(client.close())

    assert dependents == []
