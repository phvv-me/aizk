import base64
import html
import json
import secrets
from urllib.parse import urlencode

import httpx
from loguru import logger
from patos import FrozenModel
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from ..config import settings
from .server import server

# cookie the login redirect drops the random CSRF state into, read back and cleared on callback so
# a forged callback carrying someone else's code cannot pass the double-submit check.
STATE_COOKIE = "aizk_oauth_state"

# how long the CSRF state cookie lives, a short window covering the round trip out to Logto and
# back so a stale login attempt expires rather than lingering.
STATE_TTL_SECONDS = 600

# bytes of entropy behind the CSRF state token, wide enough that a state is unguessable.
STATE_BYTES = 32

# wall-clock ceiling on the discovery and token-exchange HTTP calls, generous for a human-paced
# login yet short enough that an unreachable issuer fails the page rather than hanging it.
OAUTH_TIMEOUT = 10.0

STYLE = (
    ":root{color-scheme:light dark}*{box-sizing:border-box}"
    "body{margin:0;min-height:100vh;display:grid;place-items:center;padding:1.5rem;"
    "font-family:system-ui,-apple-system,sans-serif;background:#f5f5f7;color:#1d1d1f}"
    ".card{max-width:34rem;width:100%;background:#fff;border-radius:14px;padding:2rem 2.25rem;"
    "box-shadow:0 1px 3px rgba(0,0,0,.12);line-height:1.55}"
    "h1{margin:0 0 .5rem;font-size:1.6rem}h2{margin:1.5rem 0 .5rem;font-size:1.1rem}"
    "p{margin:.6rem 0}"
    "code{background:rgba(0,0,0,.06);padding:.1rem .35rem;border-radius:5px;font-size:.9em}"
    "pre{background:#1d1d1f;color:#f5f5f7;padding:.9rem 1rem;border-radius:10px;overflow-x:auto}"
    "pre code{background:none;color:inherit;padding:0}"
    ".btn{display:inline-block;margin-top:.8rem;padding:.6rem 1.15rem;border-radius:9px;"
    "background:#635bff;color:#fff;text-decoration:none;font-weight:600}"
    "@media(prefers-color-scheme:dark){body{background:#111114;color:#f5f5f7}"
    ".card{background:#1c1c20;box-shadow:none;border:1px solid #2c2c32}"
    "code{background:rgba(255,255,255,.1)}}"
)


class Discovery(FrozenModel):
    """The OIDC endpoints the onboarding flow reads once from the issuer's well-known document.

    authorize: the authorization_endpoint the login redirect sends the browser to.
    token: the token_endpoint the callback exchanges its authorization code at.
    end_session: the end_session_endpoint a logout would use, kept for completeness.
    """

    authorize: str
    token: str
    end_session: str


class Identity(FrozenModel):
    """The slice of the id_token the quickstart greeting reads, decoded without a signature check.

    subject: the token `sub`, the stable id `store.identity.user_uuid` derives the aizk user from.
    name: a human display name from the token, the `sub` itself when the token carries none.
    """

    subject: str
    name: str


def public_base() -> str:
    """This server's public base URL, the bound localhost when none is advertised."""
    return settings.mcp_resource_url.rstrip("/") or f"http://localhost:{settings.mcp_port}"


async def discover() -> Discovery:
    """Read Logto's authorize, token, and end-session endpoints from its openid-configuration.

    Fetched per onboarding request rather than cached, since the login page is hit rarely and a
    redeploy behind the same issuer may move an endpoint.
    """
    url = f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=OAUTH_TIMEOUT) as client:
        response = await client.get(url)
    response.raise_for_status()
    doc = response.json()
    return Discovery(
        authorize=doc["authorization_endpoint"],
        token=doc["token_endpoint"],
        end_session=doc.get("end_session_endpoint", ""),
    )


def identity_from_id_token(id_token: str) -> Identity:
    """Decode a JWT's payload segment to its `sub` and display name, no signature check.

    The MCP path verifies every token it accepts, so this onboarding page only needs to read the
    id_token to greet the caller, an unverified decode of the middle segment being enough. Falls
    back through `username` to the bare `sub` when the token carries no `name`.

    id_token: the compact JWT Logto returns from the token exchange.
    """
    segment = id_token.split(".")[1]
    padded = segment + "=" * (-len(segment) % 4)
    claims: dict[str, str] = json.loads(base64.urlsafe_b64decode(padded))
    subject = claims.get("sub", "")
    name = claims.get("name") or claims.get("username") or subject
    return Identity(subject=subject, name=name)


async def redeem_code(token_endpoint: str, code: str, redirect_uri: str) -> Identity:
    """Exchange an authorization code at the token endpoint and read the caller's identity.

    Posts the RFC 6749 authorization_code grant with this server's own client credentials, confirms
    the exchange succeeded, then decodes the returned id_token. Raises on any non-2xx so the
    callback renders a clear error rather than greeting no one.

    token_endpoint: Logto's token_endpoint from discovery.
    code: the authorization code Logto handed back on the redirect.
    redirect_uri: the same callback URL the authorize request carried, echoed for the grant.
    """
    async with httpx.AsyncClient(timeout=OAUTH_TIMEOUT) as client:
        response = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
            },
        )
    response.raise_for_status()
    return identity_from_id_token(response.json()["id_token"])


def shell(title: str, body: str) -> str:
    """Wrap a page body in the shared self-contained HTML shell, inline style, no external asset.

    title: the browser-tab title.
    body: the inner HTML the shell centers in its card.
    """
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
        f'<body><main class="card">{body}</main></body></html>'
    )


def login_body() -> str:
    """The card shown when auth is on and the visitor has not logged in yet."""
    return (
        "<h1>aizk memory</h1>"
        "<p>Connect your identity through Logto to create your user. Everything after this "
        "step happens over MCP.</p>"
        '<a class="btn" href="/login">Connect with Logto</a>'
    )


def single_user_body() -> str:
    """The card shown when auth is off, where the visitor is the single local user."""
    endpoint = html.escape(f"{public_base()}/mcp")
    return (
        "<h1>aizk memory</h1>"
        "<p>Authentication is not configured, so you are the single local user with nothing to "
        f"log into. Point your MCP client at <code>{endpoint}</code> and you are set.</p>"
    )


def quickstart_body(identity: Identity) -> str:
    """The card shown after login, greeting the caller and showing how to connect their client.

    identity: the subject and display name the id_token carried.
    """
    endpoint = f"{public_base()}/mcp"
    add = html.escape(f"claude mcp add --transport http aizk {endpoint}")
    return (
        f"<h1>Welcome, {html.escape(identity.name)}</h1>"
        f"<p>Your identity <code>{html.escape(identity.subject)}</code> is now established. aizk "
        "keeps no user table, so this login is the whole account.</p>"
        "<h2>Connect your MCP client</h2>"
        f"<pre><code>{add}</code></pre>"
        "<p>On first connect your client runs its own OAuth login against this same Logto issuer "
        "and holds its own token, so there is nothing from this page to paste anywhere.</p>"
        f"<p>Endpoint: <code>{html.escape(endpoint)}</code></p>"
    )


def error_body(message: str) -> str:
    """A short error card that sends the visitor back to start the login again.

    message: the human-readable reason the login could not finish.
    """
    return (
        "<h1>Login problem</h1>"
        f"<p>{html.escape(message)}.</p>"
        '<a class="btn" href="/setup">Back to setup</a>'
    )


@server.custom_route("/", methods=["GET"])
@server.custom_route("/setup", methods=["GET"])
async def setup(request: Request) -> Response:
    """The single onboarding page: a Logto login when auth is on, a single-user note when off.

    request: the inbound Starlette request, unread since the page has no per-request state.
    """
    body = login_body() if settings.oidc_issuer else single_user_body()
    return HTMLResponse(shell("aizk", body))


@server.custom_route("/login", methods=["GET"])
async def login(request: Request) -> Response:
    """Start the OAuth code flow, stashing a CSRF state cookie then redirecting to Logto.

    request: the inbound Starlette request, unread since the authorize URL is built from settings.
    """
    if not settings.oidc_issuer:
        return RedirectResponse("/setup", status_code=302)
    discovery = await discover()
    state = secrets.token_urlsafe(STATE_BYTES)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": f"{public_base()}/callback",
            "scope": "openid profile email",
            "state": state,
        }
    )
    response = RedirectResponse(f"{discovery.authorize}?{query}", status_code=302)
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=STATE_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@server.custom_route("/callback", methods=["GET"])
async def callback(request: Request) -> Response:
    """Finish the OAuth flow: check the CSRF state, redeem the code, greet the established user.

    request: the redirect back from Logto, carrying the `state`, the `code`, and the state cookie.
    """
    expected = request.cookies.get(STATE_COOKIE)
    if not expected or request.query_params.get("state") != expected:
        return HTMLResponse(
            shell("aizk", error_body("login state did not match, start again")), 400
        )
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse(shell("aizk", error_body("no authorization code was returned")), 400)
    discovery = await discover()
    try:
        identity = await redeem_code(discovery.token, code, f"{public_base()}/callback")
    except httpx.HTTPError as error:
        logger.warning("aizk onboarding token exchange failed: {}", error)
        return HTMLResponse(shell("aizk", error_body("token exchange failed, start again")), 400)
    logger.info("aizk onboarding established identity {}", identity.subject)
    response = HTMLResponse(shell("aizk", quickstart_body(identity)))
    response.delete_cookie(STATE_COOKIE)
    return response
