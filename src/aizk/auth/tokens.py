import functools
import uuid

from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.providers.introspection import IntrospectionTokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier

from ..config import settings
from ..store import Principal


@functools.cache
def _verifier(
    issuer: str,
    jwks_uri: str,
    introspect_url: str,
    client_id: str,
    client_secret: str,
) -> TokenVerifier | None:
    """Build the verifier for one set of Zitadel settings, memoized so repeat settings reuse it.

    Cached on the primitive settings values rather than the unhashable `Settings` object, so a
    test that monkeypatches the Zitadel fields builds its own verifier without disturbing the one
    already cached for the process's real configuration. An empty issuer means auth is off. An
    introspection url routes tokens through the live RFC 7662 round-trip, which also catches a
    token revoked before expiry, falling back to the offline JWKS check with no per-call network
    trip when absent.

    issuer: base issuer URL whose tokens are accepted, empty to leave the Zitadel path off.
    jwks_uri: JWKS endpoint the offline signature path fetches keys from.
    introspect_url: RFC 7662 introspection endpoint, empty to prefer the offline JWKS path.
    client_id: resource server client id the introspection call authenticates as.
    client_secret: resource server client secret paired with client_id.
    """
    if not issuer:
        return None
    if introspect_url:
        return IntrospectionTokenVerifier(
            introspection_url=introspect_url, client_id=client_id, client_secret=client_secret
        )
    return JWTVerifier(jwks_uri=jwks_uri, issuer=issuer)


def verifier() -> TokenVerifier | None:
    """Return the process-cached token verifier for the currently configured Zitadel settings.

    None when `zitadel_issuer` is empty, the auth-off default a personal single-user stack runs
    under.
    """
    return _verifier(
        settings.zitadel_issuer,
        settings.zitadel_jwks_url,
        settings.zitadel_introspect_url,
        settings.zitadel_client_id,
        settings.zitadel_client_secret,
    )


async def principal_for_token(token: str) -> uuid.UUID | None:
    """Validate a Zitadel bearer token and resolve it to an aizk principal, null when invalid.

    Verifies the token through the configured verifier, introspection or the offline JWKS check,
    and on a valid token maps its `sub` claim to a principal, provisioning one on first sight. An
    invalid, unverifiable, or unauthenticated (no verifier configured) token resolves to null so it
    authenticates no one and the caller falls through to the next auth source, and `is_admin` stays
    governed by the principal row aizk owns rather than any claim the token carries.

    token: the raw bearer token presented by the caller.
    """
    active_verifier = verifier()
    if active_verifier is None:
        return None
    access_token = await active_verifier.verify_token(token)
    if access_token is None:
        return None
    subject = access_token.claims.get("sub")
    if not isinstance(subject, str):
        return None
    return await Principal.for_subject(subject)
