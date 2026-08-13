from async_lru import alru_cache
from fastmcp.server.auth import AccessToken, AuthProvider, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from loguru import logger
from patos import FrozenModel
from pydantic import ValidationError

from .config import Settings, settings
from .integrations import logto as lt
from .store.identity import User


class Caller(FrozenModel):
    """One verified caller together with the raw Logto subject its token proved."""

    subject: str
    user: User


class Auth(TokenVerifier):
    """Bridge Logto OAuth identity into Aizk's PostgreSQL authorization context.

    Public requests use Logto directly and present a verified Logto resource token.
    Current organizations, roles, and permissions come from Logto on each short cache window, so
    Aizk never stores a second identity or membership database. The browser API verifies its
    raw bearer tokens through this same verifier and required-scope policy.
    """

    def __init__(self, client: lt.LogtoClient | None = None, config: Settings = settings) -> None:
        self.client = client or lt.LogtoClient(config)
        self.settings = config
        super().__init__(
            base_url=config.mcp_public_url,
            required_scopes=sorted(config.logto_required_scopes),
        )

    def provider(self) -> AuthProvider | None:
        """Advertise Logto as the authorization server, or disable auth in local mode."""
        if self.settings.logto_url is None or self.settings.mcp_public_url is None:
            return None
        return RemoteAuthProvider(
            token_verifier=self,
            authorization_servers=[self.client.issuer],
            base_url=self.settings.mcp_public_url,
            resource_base_url=self.settings.mcp_public_url,
            scopes_supported=sorted(
                self.settings.oauth_scopes | self.settings.logto_required_scopes
            ),
            challenge_scopes=sorted(self.settings.logto_required_scopes),
            resource_name="AIZK",
        )

    @alru_cache(maxsize=8)
    async def get_verifiers(self) -> tuple[JWTVerifier, ...]:
        """Cache one JWT verifier set per live `Auth` instance.

        The cache key includes `self`, and the MCP server and browser API each hold
        their own instance, so the size leaves room for several coexisting instances
        instead of letting them evict one another's discovery state on every call.
        """
        discovery = await self.client.discovery()
        return tuple(
            JWTVerifier(
                jwks_uri=str(discovery.jwks_uri),
                issuer=str(discovery.issuer).rstrip("/"),
                algorithm=algorithm,
                required_scopes=self.required_scopes,
                audience=self.settings.mcp_resource_id,
            )
            for algorithm in discovery.signing_algorithms
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """Accept a token only when a trusted algorithm verifies issuer, audience, and scopes."""
        if self.settings.logto_url is None:
            return None
        for verifier in await self.get_verifiers():
            if access := await verifier.verify_token(token):
                return access
        return None

    async def identify(self, access: AccessToken | None) -> User | None:
        """Resolve verified claims through Logto authority, failing closed on invalid identity."""
        try:
            return await self.client.user(lt.Claims(**access.claims)) if access else None
        except ValidationError as error:
            logger.warning("verified Logto token carried invalid identity claims: {}", error)
            return None

    async def resolve(self) -> User:
        """Resolve one request into personal, shared, writable, and public scope standing.

        Invalid identity claims and failed Logto authority lookups fail closed. Public
        mode falls back only to the read-only anonymous identity. Auth-off local mode
        uses the configured local user for development and operator workflows.
        """
        access = get_access_token()
        if access is None and (token := self.settings.auth_token.get_secret_value()):
            access = await self.verify_token(token)
        user = await self.identify(access)
        if user is None:
            return (
                await self.client.anonymous()
                if self.settings.logto_url is not None
                else User.private(self.settings.default_user_id)
            )
        return user

    async def bearer(self, token: str) -> Caller | None:
        """Resolve one raw Authorization bearer token exactly as the MCP surface would.

        token: bearer credential taken from the request header, possibly blank.
        """
        if self.settings.logto_url is None:
            return Caller(subject="system", user=User.private(self.settings.default_user_id))
        if not token:
            return None
        access = await self.verify_token(token)
        user = await self.identify(access)
        if access is None or user is None:
            return None
        return Caller(subject=str(access.claims["sub"]), user=user)
