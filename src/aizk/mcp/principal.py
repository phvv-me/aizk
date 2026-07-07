import os
import uuid

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context, get_http_headers
from patos import FrozenModel

from ..config import settings
from ..store import Principal as PrincipalRow

# environment variable carrying a presented Zitadel bearer token over the stdio transport, where
# there is no request header to read one from. The http transport reads the Authorization header
# instead, resolving the same way through PrincipalRow.from_token.
AUTH_TOKEN_ENV = "AIZK_AUTH_TOKEN"

# the fastmcp Context state key PrincipalMiddleware.on_call_tool stashes the resolved Principal
# under, the one slot every tool reads it back from through `current_principal`.
PRINCIPAL_STATE_KEY = "principal"


class Principal(FrozenModel):
    """The caller identity resolved once per call, threaded through Context state to every tool.

    Carries only the id a verb ever acts under, deliberately not the `store.Principal` table row
    itself, so a verb never re-queries the database for it. The server-wide admin standing that
    once gated an operational surface here is gone with that surface, moved to the CLI, and the
    group-admin standing the curation verbs still check is read from the database inside
    `Group.require_admin`, not carried on this identity.

    id: the aizk principal id the caller acts as.
    """

    id: uuid.UUID


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


async def resolve_principal() -> Principal:
    """Resolve the caller into a `Principal`, by bearer token then the transport fallback.

    Tries a Zitadel bearer token first so the multi-user identity provider is the primary path. An
    unresolved caller falls back to settings.principal on the local stdio transport, the
    single-identity default a personal stack runs under, and to the anonymous principal on the
    shared HTTP transport, where an unauthenticated stranger reads exactly the public scopes and
    writes nothing. `PrincipalMiddleware.on_call_tool` calls this once per call and every tool
    reads the result back through `current_principal` rather than resolving it again.
    """
    token = bearer_token()
    principal_id = await PrincipalRow.from_token(token) if token else None
    if principal_id is None:
        principal_id = settings.anonymous_principal_id if settings.mcp_http else settings.principal
    return Principal(id=principal_id)


def current_principal() -> Principal:
    """Read the `Principal` `PrincipalMiddleware` already resolved for this call.

    Every tool calls this instead of resolving its own caller, so the one bearer-token check and
    the one is_admin read `PrincipalMiddleware.on_call_tool` already ran cover the whole call.
    """
    principal = get_context().get_state(PRINCIPAL_STATE_KEY)
    if not isinstance(principal, Principal):
        raise ToolError("no principal resolved for this call")
    return principal


def require_identified(principal: Principal) -> Principal:
    """Refuse the anonymous principal, the gate on every write verb.

    An unauthenticated HTTP stranger reads the public scopes but owns no principal row, so letting
    a write through would only die later on a foreign key. Refusing here turns that into a clear
    read-only message instead.

    principal: the caller already resolved for this call.
    """
    if principal.id == settings.anonymous_principal_id:
        raise ToolError("anonymous callers are read-only, authenticate to write")
    return principal
