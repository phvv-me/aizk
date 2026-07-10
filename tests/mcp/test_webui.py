import base64
import json
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

import dbutil
import httpx
import pytest
from mcp_probe import const
from starlette.requests import Request
from starlette.responses import Response

import aizk.mcp.webui as webui
from aizk.config import settings
from aizk.mcp.webui import (
    Discovery,
    Identity,
    callback,
    discover,
    identity_from_id_token,
    login,
    public_base,
    redeem_code,
    setup,
)

DISCOVERY_DOC = {
    "authorization_endpoint": "https://auth.test/oidc/auth",
    "token_endpoint": "https://auth.test/oidc/token",
    "end_session_endpoint": "https://auth.test/oidc/logout",
}


class FakeResponse:
    """A stand-in httpx response carrying a fixed json payload and status.

    payload: the object `.json()` hands back, the discovery doc or the token response.
    status_code: the HTTP status `raise_for_status` reads, a non-2xx raising like httpx does.
    """

    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPError(f"status {self.status_code}")


class FakeClient:
    """A stand-in `httpx.AsyncClient` returning one canned response and recording the call.

    response: the `FakeResponse` every get/post resolves to.
    calls: a dict the client writes the requested url and posted form into, for the test to assert.
    """

    def __init__(self, response: FakeResponse, calls: dict[str, object]) -> None:
        self.response = response
        self.calls = calls

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str) -> FakeResponse:
        self.calls["get_url"] = url
        return self.response

    async def post(self, url: str, data: dict[str, str]) -> FakeResponse:
        self.calls["post_url"] = url
        self.calls["post_data"] = data
        return self.response


def patch_httpx(monkeypatch: pytest.MonkeyPatch, response: FakeResponse) -> dict[str, object]:
    """Swap `webui.httpx.AsyncClient` for a `FakeClient`, returning its recorded-calls dict."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(webui.httpx, "AsyncClient", lambda **kwargs: FakeClient(response, calls))
    return calls


def make_id_token(claims: dict[str, str]) -> str:
    """Build a compact JWT whose payload segment base64url-encodes `claims`, signature ignored."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


def request_with(query: str = "", cookies: dict[str, str] | None = None) -> Request:
    """A GET Starlette request carrying the given query string and cookies, no body."""
    headers: list[tuple[bytes, bytes]] = []
    if cookies:
        raw = "; ".join(f"{name}={value}" for name, value in cookies.items())
        headers.append((b"cookie", raw.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": query.encode(),
        "headers": headers,
    }
    return Request(scope)


def body_of(response: Response) -> str:
    """The decoded HTML body a rendered Starlette response carries."""
    return bytes(response.body).decode()


def render(handler: Callable[[Request], Awaitable[Response]], request: Request) -> Response:
    """Drive one `@custom_route` handler to its response on a fresh loop.

    The decorator types a handler as returning an `Awaitable`, so awaiting it inside this async
    wrapper hands `dbutil.run` a genuine coroutine to complete.
    """

    async def call() -> Response:
        return await handler(request)

    return dbutil.run(call())


def test_public_base_prefers_the_resource_url_then_falls_back_to_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public base is the advertised resource url without its trailing slash, else the port."""
    monkeypatch.setattr(settings, "mcp_resource_url", "https://aizk.phvv.me/")
    assert public_base() == "https://aizk.phvv.me"
    monkeypatch.setattr(settings, "mcp_resource_url", "")
    monkeypatch.setattr(settings, "mcp_port", 8000)
    assert public_base() == "http://localhost:8000"


@pytest.mark.parametrize(
    ("claims", "expected_name"),
    [
        ({"sub": "u1", "name": "Ada"}, "Ada"),
        ({"sub": "u2", "username": "ada2"}, "ada2"),
        ({"sub": "u3"}, "u3"),
    ],
    ids=["name", "username-fallback", "sub-fallback"],
)
def test_identity_from_id_token_reads_the_display_name_then_falls_back(
    claims: dict[str, str], expected_name: str
) -> None:
    """The id_token decode reads `sub` and prefers name, then username, then the subject itself."""
    identity = identity_from_id_token(make_id_token(claims))
    assert identity == Identity(subject=claims["sub"], name=expected_name)


def test_discover_reads_the_three_endpoints_from_the_well_known_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """discover fetches the issuer's openid-configuration and maps its three endpoints."""
    monkeypatch.setattr(settings, "oidc_issuer", "https://auth.test/oidc/")
    calls = patch_httpx(monkeypatch, FakeResponse(DISCOVERY_DOC))

    found = dbutil.run(discover())

    assert found == Discovery(
        authorize=DISCOVERY_DOC["authorization_endpoint"],
        token=DISCOVERY_DOC["token_endpoint"],
        end_session=DISCOVERY_DOC["end_session_endpoint"],
    )
    assert calls["get_url"] == "https://auth.test/oidc/.well-known/openid-configuration"


def test_redeem_code_posts_the_authorization_code_grant_and_decodes_the_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """redeem_code posts the grant with the server's credentials, returning the token identity."""
    monkeypatch.setattr(settings, "oidc_client_id", "aizk-web")
    monkeypatch.setattr(settings, "oidc_client_secret", "shh")
    token = make_id_token({"sub": "abc", "name": "Grace"})
    calls = patch_httpx(monkeypatch, FakeResponse({"id_token": token}))

    identity = dbutil.run(
        redeem_code("https://auth.test/oidc/token", "the-code", "https://x/callback")
    )

    assert identity == Identity(subject="abc", name="Grace")
    assert calls["post_url"] == "https://auth.test/oidc/token"
    assert calls["post_data"] == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": "https://x/callback",
        "client_id": "aizk-web",
        "client_secret": "shh",
    }


def test_redeem_code_raises_on_a_non_2xx_token_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed token exchange surfaces as an httpx error rather than a decode of an empty body."""
    patch_httpx(monkeypatch, FakeResponse({"error": "invalid_grant"}, status_code=400))
    with pytest.raises(httpx.HTTPError):
        dbutil.run(redeem_code("https://auth.test/oidc/token", "bad", "https://x/callback"))


def test_setup_shows_the_login_button_when_auth_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an issuer set, /setup offers the Logto login link and no single-user note."""
    monkeypatch.setattr(settings, "oidc_issuer", "https://auth.test/oidc")
    page = body_of(render(setup, request_with()))
    assert 'href="/login"' in page
    assert "Connect with Logto" in page
    assert "single local user" not in page


def test_setup_shows_the_single_user_note_when_auth_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no issuer, /setup drops the login and tells the lone user where the endpoint is."""
    monkeypatch.setattr(settings, "oidc_issuer", "")
    monkeypatch.setattr(settings, "mcp_resource_url", "")
    monkeypatch.setattr(settings, "mcp_port", 8000)
    page = body_of(render(setup, request_with()))
    assert "single local user" in page
    assert "http://localhost:8000/mcp" in page
    assert 'href="/login"' not in page


def test_login_redirects_to_the_authorize_url_with_the_oauth_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/login redirects to Logto's authorize with the code-flow params and drops a state cookie."""
    monkeypatch.setattr(settings, "oidc_issuer", "https://auth.test/oidc")
    monkeypatch.setattr(settings, "oidc_client_id", "aizk-web")
    monkeypatch.setattr(settings, "mcp_resource_url", "https://aizk.phvv.me")
    monkeypatch.setattr(
        webui,
        "discover",
        const(
            Discovery(
                **{
                    "authorize": DISCOVERY_DOC["authorization_endpoint"],
                    "token": DISCOVERY_DOC["token_endpoint"],
                    "end_session": DISCOVERY_DOC["end_session_endpoint"],
                }
            )
        ),
    )

    response = render(login, request_with())

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(DISCOVERY_DOC["authorization_endpoint"] + "?")
    query = parse_qs(urlsplit(location).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["aizk-web"]
    assert query["redirect_uri"] == ["https://aizk.phvv.me/callback"]
    assert query["scope"] == ["openid profile email"]
    cookie = response.headers["set-cookie"]
    assert webui.STATE_COOKIE in cookie
    assert "httponly" in cookie.lower()
    assert query["state"][0] in cookie


def test_login_redirects_home_when_auth_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no issuer there is nothing to log into, so /login bounces back to /setup."""
    monkeypatch.setattr(settings, "oidc_issuer", "")
    response = render(login, request_with())
    assert response.status_code == 302
    assert response.headers["location"] == "/setup"


@pytest.mark.parametrize(
    "cookies",
    [None, {webui.STATE_COOKIE: "other"}],
    ids=["no-cookie", "mismatched-cookie"],
)
def test_callback_rejects_a_missing_or_mismatched_state(cookies: dict[str, str] | None) -> None:
    """A callback whose state does not match its cookie is refused before any token exchange."""
    response = render(callback, request_with("state=s&code=c", cookies))
    assert response.status_code == 400
    assert "state did not match" in body_of(response)


def test_callback_rejects_a_missing_authorization_code() -> None:
    """A state-valid callback carrying no code cannot exchange, so it reports the missing code."""
    request = request_with("state=s", {webui.STATE_COOKIE: "s"})
    response = render(callback, request)
    assert response.status_code == 400
    assert "no authorization code" in body_of(response)


def test_callback_renders_the_quickstart_for_a_good_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid callback greets the identity, shows the connect command, clears the state cookie."""
    monkeypatch.setattr(settings, "mcp_resource_url", "https://aizk.phvv.me")
    monkeypatch.setattr(
        webui,
        "discover",
        const(Discovery(authorize="a", token="https://auth.test/oidc/token", end_session="e")),
    )
    monkeypatch.setattr(webui, "redeem_code", const(Identity(subject="sub-123", name="Ada")))

    request = request_with("state=s&code=c", {webui.STATE_COOKIE: "s"})
    response = render(callback, request)

    assert response.status_code == 200
    page = body_of(response)
    assert "Welcome, Ada" in page
    assert "sub-123" in page
    assert "claude mcp add --transport http aizk https://aizk.phvv.me/mcp" in page
    delete = response.headers["set-cookie"]
    assert webui.STATE_COOKIE in delete
    assert "max-age=0" in delete.lower() or "01 jan 1970" in delete.lower()


def test_callback_reports_a_failed_token_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token exchange that raises is turned into a clean 400 error page, not a stack trace."""
    monkeypatch.setattr(
        webui,
        "discover",
        const(Discovery(authorize="a", token="https://auth.test/oidc/token", end_session="e")),
    )

    async def boom(*args: object, **kwargs: object) -> Identity:
        raise httpx.HTTPError("token endpoint said no")

    monkeypatch.setattr(webui, "redeem_code", boom)
    request = request_with("state=s&code=c", {webui.STATE_COOKIE: "s"})
    response = render(callback, request)
    assert response.status_code == 400
    assert "token exchange failed" in body_of(response)
