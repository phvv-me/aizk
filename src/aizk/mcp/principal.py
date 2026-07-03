import os
import uuid

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers

from ..auth import principal_for_token
from ..config import settings
from ..store import Principal

# environment variable carrying a presented Zitadel bearer token over the stdio transport, where
# there is no request header to read one from. The http transport reads the Authorization header
# instead, resolving the same way through principal_for_token.
AUTH_TOKEN_ENV = "AIZK_AUTH_TOKEN"

# tag marking the admin-only operational tools, which PrincipalMiddleware.on_list_tools filters
# from a non-admin listing so a regular user never sees the operational surface.
ADMIN_TAG = "admin"


def bearer_token() -> str | None:
    """Read a presented Zitadel bearer token, from the environment or an HTTP Authorization header.

    Prefers the AIZK_AUTH_TOKEN environment variable carried over the stdio transport, and falls
    back to the bearer scheme of the Authorization header on the HTTP transport, returning null
    when neither carries one so the caller drops to the next auth source.
    """
    token = os.environ.get(AUTH_TOKEN_ENV)
    if token:
        return token
    scheme, _, value = get_http_headers().get("authorization", "").partition(" ")
    return value if scheme.lower() == "bearer" and value else None


async def caller_principal() -> uuid.UUID:
    """Resolve the principal the current MCP call acts as, by bearer token then the fallback.

    Tries a Zitadel bearer token first so the multi-user identity provider is the primary path. An
    unresolved caller falls back to settings.principal on the local stdio transport, the
    single-identity default a personal stack runs under, and to the anonymous principal on the
    shared HTTP transport, where an unauthenticated stranger reads exactly the public scopes and
    writes nothing.
    """
    token = bearer_token()
    if token:
        principal = await principal_for_token(token)
        if principal is not None:
            return principal
    return settings.anonymous_principal_id if settings.mcp_http else settings.principal


async def require_admin() -> uuid.UUID:
    """Resolve the calling principal and refuse a non-admin, the gate every admin call runs.

    Every call, protocol-routed or a direct `tool.run()`, resolves the caller through
    `caller_principal` itself, the same per-call resolution the middleware already ran once for
    listing purposes, so a second resolution here costs nothing new and needs no cached state to
    stay in sync with.
    """
    principal_id = await caller_principal()
    if not await Principal.administers(principal_id):
        raise ToolError("aizk admin tools require an admin principal")
    return principal_id


async def require_identified() -> uuid.UUID:
    """Resolve the calling principal and refuse the anonymous one, the gate on every write verb.

    An unauthenticated HTTP stranger reads the public scopes but owns no principal row, so letting
    a write through would only die later on a foreign key. Refusing here turns that into a clear
    read-only message instead.
    """
    principal_id = await caller_principal()
    if principal_id == settings.anonymous_principal_id:
        raise ToolError("anonymous callers are read-only, authenticate to write")
    return principal_id
