import asyncio
import time
from collections.abc import Awaitable
from typing import Literal, Protocol
from urllib.parse import quote

import httpx
from async_lru import alru_cache
from loguru import logger
from pydantic import TypeAdapter, ValidationError
from pydantic.networks import AnyHttpUrl
from pydantic.types import JsonValue
from pydantic_ai.retries import AsyncTenacityTransport, RetryConfig, wait_retry_after
from tenacity import (
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from ...config import Settings, settings
from ...store.identity import OrganizationMember, OrganizationStanding, User
from .models import Account, Claims, Discovery, Member, Org, OrganizationScope, Role, Token

_ACCOUNT = TypeAdapter(Account)
_ACCOUNTS = TypeAdapter(tuple[Account, ...])
_ORGANIZATIONS = TypeAdapter(tuple[Org, ...])
_MEMBERS = TypeAdapter(tuple[Member, ...])
_ORGANIZATION_SCOPES = TypeAdapter(tuple[OrganizationScope, ...])
_ROLES = TypeAdapter(tuple[Role, ...])


class LogtoAccessError(PermissionError):
    """The current Logto account may not use the AIZK browser application."""


class SnapshotCache(Protocol):
    """The alru cache surface the registry needs for eviction."""

    def cache_invalidate(self, *args: str) -> bool: ...
    def cache_clear(self) -> None: ...


class SnapshotCaches:
    """Registry of the Logto freshness caches grouped by their eviction key."""

    def __init__(
        self,
        subject: tuple[SnapshotCache, ...],
        organization: tuple[SnapshotCache, ...],
        catalog: tuple[SnapshotCache, ...],
        tenant: tuple[SnapshotCache, ...],
    ) -> None:
        self.subject = subject
        self.organization = organization
        self.catalog = catalog
        self.tenant = tenant

    def invalidate(self, *subjects: str, organization_ids: tuple[str, ...] = ()) -> None:
        """Evict every authority snapshot affected by a Management API mutation."""
        for subject in set(subjects):
            for cache in self.subject:
                cache.cache_invalidate(subject)
        for organization_id in set(organization_ids):
            for cache in self.organization:
                cache.cache_invalidate(organization_id)
        for cache in self.catalog:
            cache.cache_invalidate()

    def invalidate_all(self) -> None:
        """Evict every cached snapshot after policy mutations reshape the whole tenant."""
        for cache in (*self.subject, *self.organization, *self.catalog, *self.tenant):
            cache.cache_clear()


class LogtoClient:
    """Logto OIDC and Management API client.

    Directory reads take `fresh=True` to bypass the freshness caches and raise on
    failure, the flavor browser logins and management decisions require. Default reads
    serve the cached snapshot and fail closed when Logto cannot be reached.
    """

    def __init__(self, config: Settings = settings) -> None:
        self.settings = config
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
            timeout=httpx.Timeout(self.settings.logto_http_timeout),
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        self._token: Token | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self.caches = SnapshotCaches(
            subject=(self._cached_user_orgs, self._cached_account, self._cached_user_roles),
            organization=(self._cached_organization_members,),
            catalog=(self._cached_organizations, self._cached_public_orgs),
            tenant=(self._cached_organization_roles,),
        )

    @property
    def issuer(self) -> AnyHttpUrl:
        """Return the trusted issuer derived from the configured Logto endpoint."""
        if self.settings.logto_url is None:
            raise RuntimeError("Logto requires a tenant endpoint")
        return AnyHttpUrl(str(httpx.URL(str(self.settings.logto_url)).join("oidc")).rstrip("/"))

    @property
    def management_url(self) -> httpx.URL:
        """Return the Management API URL rooted at the configured Logto endpoint."""
        if self.settings.logto_url is None:
            raise RuntimeError("Logto requires a tenant endpoint")
        return httpx.URL(str(self.settings.logto_url))

    @alru_cache(maxsize=1)
    async def discovery(self) -> Discovery:
        """Read and cache this tenant's verified OIDC discovery document."""
        issuer = str(self.issuer)
        url = f"{issuer}/.well-known/openid-configuration"
        discovery = Discovery(**(await self.http.get(url)).json())
        if str(discovery.issuer).rstrip("/") != issuer:
            raise ValueError("Logto discovery returned a different issuer")
        return discovery

    async def user_orgs(self, subject: str, *, fresh: bool = False) -> tuple[Org, ...]:
        """Return one user's organizations, members, roles, and permissions from Logto."""
        if self.settings.logto_url is None:
            return ()
        if fresh:
            return await self._load_user_orgs(subject, fresh=True)
        return await self._cached_user_orgs(subject)

    @alru_cache(maxsize=1024, ttl=settings.logto_cache_seconds)
    async def _cached_user_orgs(self, subject: str) -> tuple[Org, ...]:
        """Cache one subject's organization authority, holding a failed refresh closed."""
        return await self._closed(self._load_user_orgs(subject, fresh=False), (), "user authority")

    async def account(self, subject: str, *, fresh: bool = False) -> Account | None:
        """Return one directory-safe Logto user record without credentials or identifiers."""
        if self.settings.logto_url is None:
            return None
        if fresh:
            return await self._load_account(subject)
        return await self._cached_account(subject)

    @alru_cache(maxsize=1024, ttl=settings.logto_cache_seconds)
    async def _cached_account(self, subject: str) -> Account | None:
        """Cache one account snapshot, holding a failed refresh as absent."""
        return await self._closed(self._load_account(subject), None, "user directory")

    async def user_roles(self, subject: str, *, fresh: bool = False) -> tuple[Role, ...]:
        """Return the user's current tenant-level roles from Logto."""
        if self.settings.logto_url is None:
            return ()
        if fresh:
            return await self.pages(f"api/users/{quote(subject, safe='')}/roles", _ROLES)
        return await self._cached_user_roles(subject)

    @alru_cache(maxsize=1024, ttl=settings.logto_cache_seconds)
    async def _cached_user_roles(self, subject: str) -> tuple[Role, ...]:
        """Cache one subject's tenant roles, holding a failed refresh closed."""
        return await self._closed(self.user_roles(subject, fresh=True), (), "user role")

    async def organizations(self, *, fresh: bool = False) -> tuple[Org, ...]:
        """Return current Logto organizations for server-side administration."""
        if fresh:
            return await self.pages("api/organizations", _ORGANIZATIONS)
        return await self._cached_organizations()

    @alru_cache(maxsize=1, ttl=settings.logto_cache_seconds)
    async def _cached_organizations(self) -> tuple[Org, ...]:
        """Cache the organization catalog for the freshness window."""
        return await self.organizations(fresh=True)

    async def organization_roles(self, *, fresh: bool = False) -> tuple[Role, ...]:
        """Return the shared organization role template from Logto."""
        if fresh:
            return await self.pages("api/organization-roles", _ROLES)
        return await self._cached_organization_roles()

    @alru_cache(maxsize=1, ttl=settings.logto_cache_seconds)
    async def _cached_organization_roles(self) -> tuple[Role, ...]:
        """Cache the organization role template for the freshness window."""
        return await self.organization_roles(fresh=True)

    async def account_by_email(self, email: str) -> Account | None:
        """Resolve one exact email without exposing tenant-wide directory search."""
        response = await self.management(
            "GET",
            "api/users",
            params={
                "search.primaryEmail": email,
                "mode.primaryEmail": "exact",
                "page_size": 2,
            },
        )
        matches = tuple(
            account
            for account in _ACCOUNTS.validate_python(response.json())
            if account.primary_email is not None
            and account.primary_email.casefold() == email.casefold()
        )
        return matches[0] if len(matches) == 1 else None

    async def organization_members(
        self, organization_id: str, *, fresh: bool = False
    ) -> tuple[Member, ...]:
        """Return directory-safe members and their organization roles from Logto."""
        if self.settings.logto_url is None:
            return ()
        if fresh:
            path = f"api/organizations/{quote(organization_id, safe='')}/users"
            return await self.pages(path, _MEMBERS)
        return await self._cached_organization_members(organization_id)

    @alru_cache(maxsize=1024, ttl=settings.logto_cache_seconds)
    async def _cached_organization_members(self, organization_id: str) -> tuple[Member, ...]:
        """Cache one organization member directory, holding a failed refresh closed."""
        return await self._closed(
            self.organization_members(organization_id, fresh=True), (), "organization member"
        )

    async def user_scopes(
        self, subject: str, organization_id: str, *, fresh: bool = False
    ) -> tuple[OrganizationScope, ...]:
        """Return Logto's effective organization permissions for one member.

        Default reads only hold a failed load closed and stay uncached on purpose: the
        one caller is `user_orgs`, whose cached organization snapshot already carries
        these permissions for the freshness window.
        """
        scopes = self._load_user_scopes(subject, organization_id)
        if fresh:
            return await scopes
        return await self._closed(scopes, (), "organization permission")

    async def public_orgs(self, *, fresh: bool = False) -> tuple[Org, ...]:
        """Return public organization summaries without exposing their member directories."""
        if self.settings.logto_url is None:
            return ()
        if fresh:
            return await self._load_public_orgs(fresh=True)
        return await self._cached_public_orgs()

    @alru_cache(maxsize=1, ttl=settings.logto_cache_seconds)
    async def _cached_public_orgs(self) -> tuple[Org, ...]:
        """Cache the public organization catalog, holding a failed refresh closed."""
        return await self._closed(self._load_public_orgs(fresh=False), (), "public organization")

    async def _load_public_orgs(self, *, fresh: bool) -> tuple[Org, ...]:
        """Filter the organization catalog down to the exact public flag."""
        catalog = await self.organizations(fresh=fresh)
        return tuple(organization for organization in catalog if organization.is_public())

    async def user(self, claims: Claims) -> User:
        """Resolve verified claims into current Aizk read and write authority."""
        return await self.user_subject(
            claims.sub,
            name=claims.name,
            preferred_username=claims.preferred_username,
            username=claims.username,
        )

    async def user_subject(
        self,
        subject: str,
        *,
        fresh: bool = False,
        name: str | None = None,
        preferred_username: str | None = None,
        username: str | None = None,
    ) -> User:
        """Resolve one verified Logto subject into current read and write authority.

        subject: raw Logto subject to resolve.
        fresh: bypass authority caches and reject deleted, suspended, or unassigned users.
        name: fallback display name from already verified claims.
        preferred_username: fallback username from already verified claims.
        username: last-resort username from already verified claims.
        """
        # The account loads first so a fresh read screens a deleted or suspended user as
        # LogtoAccessError before a dependent endpoint can 404 on the missing subject.
        account = await self.account(subject, fresh=fresh)
        if fresh:
            self._screen_account(account)
        member_orgs, public_orgs, roles = await asyncio.gather(
            self.user_orgs(subject, fresh=fresh),
            self.public_orgs(fresh=fresh),
            self.user_roles(subject, fresh=fresh),
        )
        if fresh:
            self._screen_roles(roles)
        return self._authorized_user(
            subject,
            member_orgs,
            public_orgs,
            account,
            roles,
            name,
            preferred_username,
            username,
        )

    @staticmethod
    def _screen_account(account: Account | None) -> None:
        """Reject a deleted or suspended account for fresh browser authority."""
        if account is None:
            raise LogtoAccessError("Logto account no longer exists")
        if account.is_suspended:
            raise LogtoAccessError("Logto account is suspended")

    def _screen_roles(self, roles: tuple[Role, ...]) -> None:
        """Reject an account without the AIZK user role for fresh browser authority."""
        if not any(role.name == self.settings.logto_user_role for role in roles):
            raise LogtoAccessError("Logto account lacks AIZK application access")

    def _authorized_user(
        self,
        subject: str,
        member_orgs: tuple[Org, ...],
        public_orgs: tuple[Org, ...],
        account: Account | None,
        roles: tuple[Role, ...],
        name: str | None = None,
        preferred_username: str | None = None,
        username: str | None = None,
    ) -> User:
        """Build one AIZK identity from an already selected Logto authority snapshot."""
        organizations = self._merged(member_orgs, public_orgs)
        if account is not None:
            name, username, avatar = account.name, account.username, account.avatar
        else:
            username, avatar = preferred_username or username, None
        user_id = self.settings.subject_id(subject)
        return User.authorized(
            user_id,
            read=(
                user_id,
                *(self.settings.scope_id(organization_id) for organization_id in organizations),
            ),
            write=(
                user_id,
                *(
                    self.settings.scope_id(organization.id)
                    for organization in member_orgs
                    if organization.permits(self.settings.logto_write_permission)
                ),
            ),
            public=(self.settings.scope_id(organization.id) for organization in public_orgs),
            name=name,
            username=username,
            avatar=avatar,
            roles=(role.name for role in roles),
            organizations=(self.standing(organization) for organization in organizations.values()),
        )

    @staticmethod
    def _merged(member_orgs: tuple[Org, ...], public_orgs: tuple[Org, ...]) -> dict[str, Org]:
        """Overlay public catalog descriptions onto the caller's member organizations."""
        organizations = {organization.id: organization for organization in member_orgs}
        for public in public_orgs:
            member = organizations.get(public.id)
            organizations[public.id] = (
                public
                if member is None
                else member.model_copy(
                    update={
                        "custom_data": public.custom_data,
                        "description": public.description,
                    }
                )
            )
        return organizations

    async def anonymous(self) -> User:
        """Resolve the anonymous fallback into only current public read authority."""
        organizations = await self.public_orgs()
        return User.authorized(
            self.settings.anonymous_user_id,
            public=(self.settings.scope_id(organization.id) for organization in organizations),
            organizations=(self.standing(organization) for organization in organizations),
        )

    @staticmethod
    async def _closed[T](read: Awaitable[T], fallback: T, authority: str) -> T:
        """Fail closed with `fallback` when a cached Logto `authority` read cannot refresh."""
        try:
            return await read
        except (httpx.HTTPError, ValidationError, ValueError) as error:
            logger.warning("Logto {} refresh failed and closed access: {}", authority, error)
            return fallback

    async def _load_user_orgs(self, subject: str, *, fresh: bool) -> tuple[Org, ...]:
        """Load one subject's organizations and their effective member details."""
        path = f"api/users/{quote(subject, safe='')}/organizations"
        organizations = await self.pages(path, _ORGANIZATIONS)
        details = await asyncio.gather(
            *(
                asyncio.gather(
                    self.user_scopes(subject, organization.id, fresh=fresh),
                    self.organization_members(organization.id, fresh=fresh),
                )
                for organization in organizations
            )
        )
        return tuple(
            organization.model_copy(update={"scopes": scopes, "members": members})
            for organization, (scopes, members) in zip(organizations, details, strict=True)
        )

    async def _load_account(self, subject: str) -> Account | None:
        """Load one account while treating a confirmed missing record as absent."""
        path = f"api/users/{quote(subject, safe='')}"
        try:
            response = await self.management("GET", path)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return None
            raise
        return _ACCOUNT.validate_python(response.json())

    async def _load_user_scopes(
        self, subject: str, organization_id: str
    ) -> tuple[OrganizationScope, ...]:
        """Load one member's effective organization permissions."""
        path = (
            f"api/organizations/{quote(organization_id, safe='')}/users/"
            f"{quote(subject, safe='')}/scopes"
        )
        response = await self.management("GET", path)
        return _ORGANIZATION_SCOPES.validate_python(response.json())

    def standing(self, organization: Org) -> OrganizationStanding:
        """Map one validated Logto organization into AIZK's directory-safe identity model."""
        return OrganizationStanding(
            id=self.settings.scope_id(organization.id),
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
            writable=organization.permits(self.settings.logto_write_permission),
        )

    async def management(
        self,
        method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        payload: dict[str, JsonValue] | None = None,
    ) -> httpx.Response:
        """Call one Logto Management API endpoint with the cached M2M credential.

        method: HTTP operation supported by the Management API.
        path: tenant-relative endpoint such as `api/organization-scopes`.
        params: optional query parameters.
        payload: optional JSON object.
        """
        response = await self.http.request(
            method,
            str(self.management_url.join(path)),
            params=params,
            json=payload,
            headers={"Authorization": f"Bearer {await self._access_token()}"},
        )
        response.raise_for_status()
        return response

    async def pages[T](self, path: str, adapter: TypeAdapter[tuple[T, ...]]) -> tuple[T, ...]:
        """Read every 100-item Management API page and validate one typed tuple."""
        items: list[T] = []
        page = 1
        while True:
            response = await self.management(
                "GET",
                path,
                params={"page": page, "page_size": 100},
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
            # Replaying a client-credentials grant only mints another token, so the
            # token POST opts back into the idempotent-only retry policy.
            response = await self.http.post(
                str(discovery.token_endpoint),
                auth=(
                    self.settings.logto_client_id,
                    self.settings.logto_client_secret.get_secret_value(),
                ),
                data={
                    "grant_type": "client_credentials",
                    "resource": str(self.settings.logto_management_resource),
                    "scope": "all",
                },
                extensions={"retryable": True},
            )
            self._token = Token(**response.json())
            self._token_expires_at = now + max(1, self._token.expires_in - 30)
            return self._token.access_token

    @staticmethod
    def _retryable(error: BaseException) -> bool:
        """Retry idempotent requests on transport errors and transient HTTP responses.

        A timeout can outlive a request the server already processed, so replaying a
        creation POST would duplicate resources. Only idempotent methods retry by
        default; a request whose replay is provably safe opts in via the `retryable`
        extension.
        """
        if not isinstance(error, httpx.RequestError | httpx.HTTPStatusError):
            return False
        request = error.request
        idempotent = request.method in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
        if request.extensions.get("retryable", idempotent) is not True:
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
