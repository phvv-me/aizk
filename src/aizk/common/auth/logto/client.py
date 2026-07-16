import asyncio
import time
from urllib.parse import quote

import httpx
from async_lru import alru_cache
from loguru import logger
from pydantic import TypeAdapter, ValidationError
from pydantic.networks import AnyHttpUrl
from pydantic_ai.retries import AsyncTenacityTransport, RetryConfig, wait_retry_after
from tenacity import (
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from ....config import settings
from ....store.identity import OrganizationMember, OrganizationStanding, User
from .models import Account, Claims, Discovery, Member, Org, OrganizationScope, Role, Token

_ACCOUNT = TypeAdapter(Account)
_ORGANIZATIONS = TypeAdapter(tuple[Org, ...])
_MEMBERS = TypeAdapter(tuple[Member, ...])
_ORGANIZATION_SCOPES = TypeAdapter(tuple[OrganizationScope, ...])
_ROLES = TypeAdapter(tuple[Role, ...])


class LogtoClient:
    """Logto OIDC and Management API client."""

    def __init__(self) -> None:
        retry = RetryConfig(
            retry=retry_if_exception(self._retryable),
            wait=wait_retry_after(
                fallback_strategy=wait_random_exponential(
                    multiplier=0.25,
                    max=2.0,
                ),
                max_wait=2.0,
            ),
            stop=stop_after_attempt(3),
            before_sleep=self._log_retry,
            reraise=True,
        )
        self.http = httpx.AsyncClient(
            transport=AsyncTenacityTransport(
                config=retry,
                wrapped=httpx.AsyncHTTPTransport(
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10)
                ),
                validate_response=httpx.Response.raise_for_status,
            ),
            timeout=httpx.Timeout(settings.logto_http_timeout),
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        self._token: Token | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def issuer(self) -> AnyHttpUrl:
        """Return the trusted issuer derived from the configured Logto endpoint."""
        if settings.logto_url is None:
            raise RuntimeError("Logto requires a tenant endpoint")
        return AnyHttpUrl(str(httpx.URL(str(settings.logto_url)).join("oidc")).rstrip("/"))

    @property
    def management_url(self) -> httpx.URL:
        """Return the Management API URL rooted at the configured Logto endpoint."""
        if settings.logto_url is None:
            raise RuntimeError("Logto requires a tenant endpoint")
        return httpx.URL(str(settings.logto_url))

    @alru_cache(maxsize=1)
    async def discovery(self) -> Discovery:
        """Read and cache this tenant's verified OIDC discovery document."""
        issuer = str(self.issuer)
        url = f"{issuer}/.well-known/openid-configuration"
        discovery = Discovery(**(await self.http.get(url)).json())
        if str(discovery.issuer).rstrip("/") != issuer:
            raise ValueError("Logto discovery returned a different issuer")
        return discovery

    @alru_cache(maxsize=1024, ttl=settings.logto_cache_seconds)
    async def user_orgs(self, subject: str) -> tuple[Org, ...]:
        """Return one user's organizations, members, roles, and permissions from Logto."""
        if settings.logto_url is None:
            return ()
        try:
            path = f"api/users/{quote(subject, safe='')}/organizations"
            organizations = await self._pages(path, _ORGANIZATIONS)
            details = await asyncio.gather(
                *(
                    asyncio.gather(
                        self.user_scopes(subject, organization.id),
                        self.organization_members(organization.id),
                    )
                    for organization in organizations
                )
            )
            return tuple(
                organization.model_copy(update={"scopes": scopes, "members": members})
                for organization, (scopes, members) in zip(organizations, details, strict=True)
            )
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            logger.warning("Logto user authority refresh failed and closed access: {}", error)
            return ()

    @alru_cache(maxsize=1024, ttl=settings.logto_cache_seconds)
    async def account(self, subject: str) -> Account | None:
        """Return one directory-safe Logto user record without credentials or identifiers."""
        if settings.logto_url is None:
            return None
        try:
            path = f"api/users/{quote(subject, safe='')}"
            response = await self.http.get(
                str(self.management_url.join(path)),
                headers={"Authorization": f"Bearer {await self._access_token()}"},
            )
            return _ACCOUNT.validate_python(response.json())
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            logger.warning("Logto user directory refresh failed: {}", error)
            return None

    @alru_cache(maxsize=1024, ttl=settings.logto_cache_seconds)
    async def user_roles(self, subject: str) -> tuple[Role, ...]:
        """Return the user's current tenant-level roles from Logto."""
        if settings.logto_url is None:
            return ()
        try:
            return await self._pages(f"api/users/{quote(subject, safe='')}/roles", _ROLES)
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            logger.warning("Logto user role refresh failed: {}", error)
            return ()

    @alru_cache(maxsize=1024, ttl=settings.logto_cache_seconds)
    async def organization_members(self, organization_id: str) -> tuple[Member, ...]:
        """Return directory-safe members and their organization roles from Logto."""
        if settings.logto_url is None:
            return ()
        try:
            path = f"api/organizations/{quote(organization_id, safe='')}/users"
            return await self._pages(path, _MEMBERS)
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            logger.warning("Logto organization member refresh failed: {}", error)
            return ()

    async def user_scopes(
        self, subject: str, organization_id: str
    ) -> tuple[OrganizationScope, ...]:
        """Return Logto's effective organization permissions for one member."""
        try:
            path = (
                f"api/organizations/{quote(organization_id, safe='')}/users/"
                f"{quote(subject, safe='')}/scopes"
            )
            response = await self.http.get(
                str(self.management_url.join(path)),
                headers={"Authorization": f"Bearer {await self._access_token()}"},
            )
            return _ORGANIZATION_SCOPES.validate_python(response.json())
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            logger.warning("Logto organization permission refresh failed closed: {}", error)
            return ()

    @alru_cache(maxsize=1, ttl=settings.logto_cache_seconds)
    async def public_orgs(self) -> tuple[Org, ...]:
        """Return Logto organizations explicitly marked public, failing closed."""
        if settings.logto_url is None:
            return ()
        try:
            organizations = tuple(
                organization
                for organization in await self._pages("api/organizations", _ORGANIZATIONS)
                if organization.is_public()
            )
            members = await asyncio.gather(
                *(self.organization_members(organization.id) for organization in organizations)
            )
            return tuple(
                organization.model_copy(update={"members": directory})
                for organization, directory in zip(organizations, members, strict=True)
            )
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            logger.warning("Logto public organization refresh failed and closed access: {}", error)
            return ()

    async def user(self, claims: Claims) -> User:
        """Resolve verified claims into current Aizk read and write authority."""
        organizations, account, roles = await asyncio.gather(
            self.user_orgs(claims.sub),
            self.account(claims.sub),
            self.user_roles(claims.sub),
        )
        readable = {
            organization.id: settings.scope_id(organization.id) for organization in organizations
        }
        user_id = settings.subject_id(claims.sub)
        return User.authorized(
            user_id,
            read=(user_id, *readable.values()),
            write=(
                user_id,
                *(
                    readable[organization.id]
                    for organization in organizations
                    if organization.permits(settings.logto_write_permission)
                ),
            ),
            name=account.name if account is not None else claims.name,
            username=(
                account.username
                if account is not None
                else claims.preferred_username or claims.username
            ),
            avatar=account.avatar if account is not None else None,
            roles=(role.name for role in roles),
            organizations=(self.standing(organization) for organization in organizations),
        )

    @staticmethod
    def standing(organization: Org) -> OrganizationStanding:
        """Map one validated Logto organization into AIZK's directory-safe identity model."""
        return OrganizationStanding(
            id=settings.scope_id(organization.id),
            name=organization.name,
            description=organization.description,
            custom_data=organization.custom_data,
            members=tuple(
                OrganizationMember(
                    name=member.name,
                    username=member.username,
                    avatar=member.avatar,
                    roles=tuple(role.name for role in member.roles),
                )
                for member in organization.members
            ),
            roles=tuple(role.name for role in organization.roles),
            permissions=tuple(scope.name for scope in organization.scopes),
            public=organization.is_public(),
            writable=organization.permits(settings.logto_write_permission),
        )

    async def _pages[T](self, path: str, adapter: TypeAdapter[tuple[T, ...]]) -> tuple[T, ...]:
        """Read every 100-item Management API page and validate one typed tuple."""
        token = await self._access_token()
        items: list[T] = []
        page = 1
        while True:
            response = await self.http.get(
                str(self.management_url.join(path)),
                params={"page": page, "page_size": 100},
                headers={"Authorization": f"Bearer {token}"},
            )
            batch = adapter.validate_python(response.json())
            items.extend(batch)
            if len(batch) < 100:
                return tuple(items)
            page += 1

    async def _access_token(self) -> str:
        """Return a cached Management API token with an expiration margin."""
        now = time.monotonic()
        if self._token is not None and now < self._token_expires_at:
            return self._token.access_token
        async with self._token_lock:
            now = time.monotonic()
            if self._token is not None and now < self._token_expires_at:
                return self._token.access_token
            discovery = await self.discovery()
            response = await self.http.post(
                str(discovery.token_endpoint),
                auth=(
                    settings.logto_client_id,
                    settings.logto_client_secret.get_secret_value(),
                ),
                data={
                    "grant_type": "client_credentials",
                    "resource": str(settings.logto_management_resource),
                    "scope": "all",
                },
            )
            self._token = Token(**response.json())
            self._token_expires_at = now + max(1, self._token.expires_in - 30)
            return self._token.access_token

    @staticmethod
    def _retryable(error: BaseException) -> bool:
        """Retry safe requests on transport errors and transient HTTP responses."""
        if not isinstance(error, httpx.RequestError | httpx.HTTPStatusError):
            return False
        if error.request.extensions.get("retryable", True) is not True:
            return False
        if isinstance(error, httpx.TransportError):
            return True
        return isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {
            408,
            425,
            429,
            500,
            502,
            503,
            504,
        }

    @staticmethod
    def _log_retry(state: RetryCallState) -> None:
        """Record retry pressure without exposing request credentials."""
        error = state.outcome.exception() if state.outcome else None
        logger.warning("retrying Logto request after attempt {}: {}", state.attempt_number, error)

    async def close(self) -> None:
        """Close pooled Logto connections during server shutdown."""
        await self.http.aclose()
