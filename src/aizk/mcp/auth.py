from async_lru import alru_cache
from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from loguru import logger
from pydantic import ValidationError

from ..common.auth import logto as lt
from ..config import settings
from ..store.identity import User


class Auth(TokenVerifier):
    """Bridge Logto OAuth identity into Aizk's PostgreSQL authorization context.

    Public requests use FastMCP's OAuth proxy and a verified Logto resource token.
    Current organizations, roles, and permissions come from Logto on each short cache window, so
    Aizk never stores a second identity or membership database.
    """

    def __init__(self, client: lt.LogtoClient | None = None) -> None:
        self.client = client or lt.LogtoClient()
        super().__init__(
            base_url=settings.mcp_public_url,
            required_scopes=sorted(settings.logto_required_scopes),
        )

    def provider(self) -> AuthProvider | None:
        """Return the Logto-backed OAuth proxy, or no provider in explicit local mode."""
        if settings.logto_url is None or settings.mcp_public_url is None:
            return None
        resource = settings.mcp_resource_id
        provider = OIDCProxy(
            config_url=f"{self.client.issuer}/.well-known/openid-configuration",
            client_id=settings.oauth_client_id,
            client_secret=settings.oauth_client_secret.get_secret_value(),
            token_verifier=self,
            base_url=settings.mcp_public_url,
            resource_base_url=settings.mcp_public_url,
            extra_authorize_params={"prompt": "consent"},
            extra_token_params={"resource": resource},
            fastmcp_access_token_expiry_seconds=settings.oauth_reference_token_seconds,
            token_expiry_threshold_seconds=60,
        )
        provider.update_default_scopes(
            sorted(settings.oauth_scopes | settings.logto_required_scopes)
        )
        return provider

    @alru_cache(maxsize=1)
    async def get_verifiers(self) -> tuple[JWTVerifier, ...]:
        """Cache one JWT verifier for each asymmetric algorithm Logto advertises."""
        discovery = await self.client.discovery()
        return tuple(
            JWTVerifier(
                jwks_uri=str(discovery.jwks_uri),
                issuer=str(discovery.issuer).rstrip("/"),
                algorithm=algorithm,
                required_scopes=self.required_scopes,
                audience=settings.mcp_resource_id,
                http_client=self.client.http,
            )
            for algorithm in discovery.signing_algorithms
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """Accept a token only when a trusted algorithm verifies issuer, audience, and scopes."""
        if settings.logto_url is None:
            return None
        for verifier in await self.get_verifiers():
            if access := await verifier.verify_token(token):
                return access
        return None

    async def resolve(self) -> User:
        """Resolve one request into personal, shared, writable, and public scope standing.

        Invalid identity claims and failed Logto authority lookups fail closed. Public
        mode falls back only to the read-only anonymous identity. Auth-off local mode
        uses the configured local user for development and operator workflows.
        """
        access = get_access_token()
        if access is None and (token := settings.auth_token.get_secret_value()):
            access = await self.verify_token(token)
        try:
            user = await self.client.user(lt.Claims(**access.claims)) if access else None
        except ValidationError as error:
            logger.warning("verified Logto token carried invalid identity claims: {}", error)
            user = None
        if user is None:
            fallback = (
                settings.anonymous_user_id
                if settings.logto_url is not None
                else settings.default_user_id
            )
            user = User.private(fallback)
        organizations = await self.client.public_orgs()
        authority = {organization.id: organization for organization in user.organizations}
        for organization in organizations:
            scope_id = settings.scope_id(organization.id)
            current = authority.get(scope_id)
            authority[scope_id] = (
                current.model_copy(update={"public": True})
                if current is not None
                else self.client.standing(organization)
            )
        return User.authorized(
            user.id,
            read=user.scopes.read,
            write=user.scopes.write,
            public=(settings.scope_id(organization.id) for organization in organizations),
            name=user.name,
            username=user.username,
            avatar=user.avatar,
            roles=user.roles,
            organizations=authority.values(),
        )
