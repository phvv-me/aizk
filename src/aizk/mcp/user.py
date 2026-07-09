import functools
import os
import uuid
from collections.abc import Mapping, Sequence

from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.providers.introspection import IntrospectionTokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_context, get_http_headers
from loguru import logger
from patos import FrozenModel
from pydantic import AnyHttpUrl

from ..config import settings
from ..exceptions import ScopeNotFoundError
from ..store.identity import org_uuid, user_uuid

# environment variable carrying a presented OIDC bearer token over the stdio transport, where
# there is no request header to read one from. The http transport reads the Authorization header
# instead, resolving the same way through `from_token`.
AUTH_TOKEN_ENV = "AIZK_AUTH_TOKEN"

# the fastmcp Context state key IdentityMiddleware.on_call_tool stashes the resolved User
# under, the one slot every tool reads it back from through `current_user`.
USER_STATE_KEY = "user"

# Logto organization roles that carry write standing; any other role (viewer, or an unknown one)
# reads the org's scope but writes nothing, the safe default an unfamiliar role folds into.
WRITABLE_ROLES = frozenset({"editor", "admin"})


class User(FrozenModel):
    """The caller identity and org standing resolved once per call from its verified token.

    Carries everything a verb acts under so no verb ever queries the database for identity: the
    derived `owner_id`, the orgs the token places the caller in, the writable subset, and the
    org-name map a scope argument resolves through. There is no local user, org, or membership
    table behind this, the token is the whole source of truth, so a caller with no token (the
    anonymous or single-user default) carries empty standing and reads only the public scope.

    id: the aizk user id the caller acts as, `uuid5(oidc_subject)`.
    orgs: every org the token places the caller in, the reader half of its standing.
    writable_orgs: the subset the token grants editor-or-admin standing in.
    names: org display-name to org uuid, the caller's own scope vocabulary a name resolves through.
    """

    id: uuid.UUID
    orgs: tuple[uuid.UUID, ...] = ()
    writable_orgs: tuple[uuid.UUID, ...] = ()
    names: dict[str, uuid.UUID] = {}

    def scope_ids(self, scopes: str | None) -> tuple[uuid.UUID, ...]:
        """Resolve a comma-separated org-name list to the sorted scope set a write is shared with.

        Reads names out of the caller's own token vocabulary rather than a database lookup, so a
        name the token never placed the caller in fails fast rather than writing somewhere they
        cannot see, and the ids sort so `finance,business` and `business,finance` land on the
        identical canonical array every uniqueness and containment check depends on. A null or
        blank string means private to the caller, an empty tuple. Naming an org grants no write by
        itself, the RLS write policy still checks `writable_orgs`, so a viewer who names a readable
        org has its write refused in Postgres, not here.

        scopes: comma-separated org names, null or blank for private.
        """
        names = [name.strip() for name in (scopes or "").split(",") if name.strip()]
        if not names:
            return ()
        try:
            return tuple(sorted(self.names[name] for name in names))
        except KeyError as missing:
            raise ScopeNotFoundError(
                f"no scope named {missing.args[0]!r} in your orgs"
            ) from missing


def bearer_token() -> str | None:
    """Read a presented OIDC bearer token, from the environment or an HTTP Authorization header.

    Prefers the AIZK_AUTH_TOKEN environment variable carried over the stdio transport, and falls
    back to the bearer scheme of the Authorization header on the HTTP transport, returning null
    when neither carries one so the caller drops to the next auth source.
    """
    token = os.environ.get(AUTH_TOKEN_ENV)
    if token:
        return token
    scheme, _, value = get_http_headers().get("authorization", "").partition(" ")
    return value if scheme.lower() == "bearer" and value else None


@functools.cache
def cached_verifier(
    issuer: str,
    jwks_uri: str,
    introspect_url: str,
    client_id: str,
    client_secret: str,
    algorithm: str,
    required_scopes: str,
    audience: str,
) -> TokenVerifier | None:
    """Build the verifier for one set of OIDC settings, memoized so repeat settings reuse it.

    Cached on the primitive settings values rather than the unhashable `Settings` object, so a
    test that monkeypatches the OIDC fields builds its own verifier without disturbing the one
    already cached for the process's real configuration. An empty issuer means auth is off. An
    introspection url routes tokens through the live RFC 7662 round-trip, which also catches a
    token revoked before expiry, falling back to the offline JWKS check with no per-call network
    trip when absent. `verifier` forwards the live settings here; call that instead unless a test
    needs to pin a specific settings tuple.

    issuer: base issuer URL whose tokens are accepted, empty to leave the OIDC path off.
    jwks_uri: JWKS endpoint the offline signature path fetches keys from.
    introspect_url: RFC 7662 introspection endpoint, empty to prefer the offline JWKS path.
    client_id: resource server client id the introspection call authenticates as.
    client_secret: resource server client secret paired with client_id.
    algorithm: JWS algorithm the issuer signs its tokens with, matched against the token header on
        the offline JWKS path. Providers differ, Logto signs ES384 while many others default to
        RS256, so the wrong value fails every signature silently.
    required_scopes: comma-separated scopes a token must carry, also the `scopes_supported` the
        resource metadata advertises so a client requests exactly them, empty to accept any.
    audience: the RFC 8707 resource indicator a token's `aud` must equal, so a token the same
        issuer signed for a different resource is rejected here. Empty leaves `aud` unchecked.
    """
    if not issuer:
        return None
    scopes = [scope.strip() for scope in required_scopes.split(",") if scope.strip()] or None
    if introspect_url:
        # the introspection verifier takes no audience, so aud goes unchecked on this path;
        # the live deployment uses the JWKS path below, where audience is enforced.
        return IntrospectionTokenVerifier(
            introspection_url=introspect_url,
            client_id=client_id,
            client_secret=client_secret,
            required_scopes=scopes,
        )
    return JWTVerifier(
        jwks_uri=jwks_uri,
        issuer=issuer,
        algorithm=algorithm,
        required_scopes=scopes,
        audience=audience or None,
    )


def verifier() -> TokenVerifier | None:
    """Return the process-cached token verifier for the currently configured OIDC settings.

    None when `oidc_issuer` is empty, the auth-off default a personal single-user stack runs under.
    """
    return cached_verifier(
        settings.oidc_issuer,
        settings.oidc_jwks_url,
        settings.oidc_introspect_url,
        settings.oidc_client_id,
        settings.oidc_client_secret,
        settings.oidc_algorithm,
        settings.oidc_required_scopes,
        settings.mcp_resource_id,
    )


def auth_provider() -> AuthProvider | None:
    """The MCP server's auth: verify tokens, and advertise the issuer when a URL is set.

    Wrapping the token verifier in a `RemoteAuthProvider` publishes the RFC 9728 protected
    resource metadata that names the OIDC issuer as this server's authorization server, so a
    client that hits the endpoint unauthenticated is told where to log in and then obtains and
    refreshes its own tokens through the identity provider, no key to mint or paste. With no
    `mcp_resource_url` to advertise from, the bare verifier is served instead, the single-user
    path where the caller already presents a token, and none at all leaves auth off.
    """
    active = verifier()
    if active is None or not settings.mcp_resource_url:
        return active
    return RemoteAuthProvider(
        token_verifier=active,
        authorization_servers=[AnyHttpUrl(settings.oidc_issuer)],
        base_url=settings.mcp_resource_url,
        resource_name="aizk",
    )


def standing_from_claim(
    claim: object,
) -> tuple[tuple[uuid.UUID, ...], tuple[uuid.UUID, ...], dict[str, uuid.UUID]]:
    """Derive org standing from the token's organization claim: (orgs, writable_orgs, name map).

    The Logto org claim is a list of `{id, role, name}`, each a membership the token itself
    vouches for, so every named org derives a stable `uuid5` scope with no local mirror table.
    Editor and admin roles land in the writable subset; any other role, or an unfamiliar one, reads
    the scope but writes nothing. A hostile or drifted claim must never crash auth, so a malformed
    entry is logged and skipped while the rest resolve. The name map lets a write's scope argument
    resolve a human-readable org name the same token vouched for.

    claim: the value of the configured `oidc_groups_claim`, expected a list of org dicts.
    """
    if not isinstance(claim, Sequence) or isinstance(claim, str | bytes):
        return (), (), {}
    orgs: list[uuid.UUID] = []
    writable: list[uuid.UUID] = []
    names: dict[str, uuid.UUID] = {}
    for entry in claim:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str):
            logger.warning("skipping malformed org claim entry {!r}", entry)
            continue
        scope = org_uuid(entry["id"])
        orgs.append(scope)
        if entry.get("role") in WRITABLE_ROLES:
            writable.append(scope)
        name = entry.get("name")
        if isinstance(name, str):
            names[name] = scope
    return tuple(sorted(orgs)), tuple(sorted(writable)), names


async def from_token(token: str) -> User | None:
    """Validate a bearer token and resolve it to a `User` with org standing, null when invalid.

    Verifies the token through the configured verifier, introspection or the offline JWKS check,
    and on a valid token derives the caller's `owner_id` from its `sub` claim and its org standing
    from the configured `oidc_groups_claim`, both by stable `uuid5` with no database round trip. An
    invalid, unverifiable, or unauthenticated (no verifier configured) token resolves to null so it
    authenticates no one and the caller falls through to the next auth source.

    token: the raw bearer token presented by the caller.
    """
    active = verifier()
    if active is None:
        return None
    access_token = await active.verify_token(token)
    if access_token is None:
        return None
    subject = access_token.claims.get("sub")
    if not isinstance(subject, str):
        return None
    orgs, writable, names = standing_from_claim(
        access_token.claims.get(settings.oidc_groups_claim)
    )
    return User(id=user_uuid(subject), orgs=orgs, writable_orgs=writable, names=names)


async def resolve_user() -> User:
    """Resolve the caller into a `User`, by bearer token then the transport fallback.

    Tries a OIDC bearer token first so the multi-user identity provider is the primary path. An
    unresolved caller falls back to settings.default_user_id on the local stdio transport, the
    single-identity default a personal stack runs under, and to the anonymous user on the shared
    HTTP transport, where an unauthenticated stranger reads exactly the public scope and writes
    nothing, both carrying empty org standing. `IdentityMiddleware.on_call_tool` calls this once
    per call and every tool reads the result back through `current_user` rather than resolving it
    again.
    """
    token = bearer_token()
    resolved = await from_token(token) if token else None
    if resolved is not None:
        return resolved
    fallback = settings.anonymous_user_id if settings.mcp_http else settings.default_user_id
    return User(id=fallback)


def current_user() -> User:
    """Read the `User` `IdentityMiddleware` already resolved for this call.

    Every tool calls this instead of resolving its own caller, so the one bearer-token check
    `IdentityMiddleware.on_call_tool` already ran covers the whole call.
    """
    user = get_context().get_state(USER_STATE_KEY)
    if not isinstance(user, User):
        raise ToolError("no user resolved for this call")
    return user


def require_identified(user: User) -> User:
    """Refuse the anonymous user, the gate on every write verb.

    An unauthenticated HTTP stranger reads the public scope but carries no writable standing, so
    letting a write through would only be refused later by the RLS write policy. Refusing here
    turns that into a clear read-only message instead.

    user: the caller already resolved for this call.
    """
    if user.id == settings.anonymous_user_id:
        raise ToolError("anonymous callers are read-only, authenticate to write")
    return user
