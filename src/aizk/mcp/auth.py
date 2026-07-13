from async_lru import alru_cache
from fastmcp.server.auth import AccessToken, AuthProvider, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from loguru import logger
from pydantic import ValidationError

from ..common.auth import logto as lt
from ..config import settings
from ..store.identity import User


class Auth(TokenVerifier):
    """Adapt Logto identity and authority to FastMCP authentication."""

    def __init__(self, client: lt.LogtoClient | None = None) -> None:
        self.client = client or lt.LogtoClient()
        super().__init__(
            base_url=settings.mcp_public_url,
            required_scopes=sorted(settings.logto_required_scopes),
        )

    def provider(self) -> AuthProvider | None:
        """Expose Logto as FastMCP's remote authorization server."""
        if settings.logto_url is None or settings.mcp_public_url is None:
            return None
        return RemoteAuthProvider(
            token_verifier=self,
            authorization_servers=[self.client.issuer],
            base_url=settings.mcp_public_url,
            resource_name="aizk",
        )

    @alru_cache(maxsize=1)
    async def get_verifiers(self) -> tuple[JWTVerifier, ...]:
        """Build verifiers from the trusted Logto discovery document once."""
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
        """Verify a resource token against the algorithms trusted by Logto discovery."""
        if settings.logto_url is None:
            return None
        for verifier in await self.get_verifiers():
            if access := await verifier.verify_token(token):
                return access
        return None

    async def resolve(self) -> User:
        """Resolve the caller with fail-closed public organization metadata."""
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
        return user.model_copy(
            update={
                "scopes": user.scopes.model_copy(
                    update={
                        "public": frozenset(
                            settings.scope_id(organization.id) for organization in organizations
                        )
                    }
                ),
            }
        )
