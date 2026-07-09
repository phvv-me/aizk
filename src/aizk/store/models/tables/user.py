import functools
import uuid
from typing import TYPE_CHECKING, Self

from fastmcp.server.auth import AuthProvider, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.providers.introspection import IntrospectionTokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl
from sqlalchemy import Text, select
from sqlmodel import Field, Relationship

from ....config import settings
from ...engine import acting_as, as_system, session
from ...mixins import Id, TableBase, Timestamped
from .document import Document

if TYPE_CHECKING:
    from .membership import Membership


class User(Id, Timestamped, TableBase, table=True):
    """An actor that can own and read memory, a human or an agent identity.

    id: stable identity, generated client-side on insert.
    display_name: human-readable label when one is known.
    oidc_subject: unique subject claim from the identity provider, null until linked.
    is_admin: whether this user is the engine admin. It is set only on the seeded system user
        and never granted to a client, since the operator is the Postgres owner role the CLI acts
        as, not an app user promoted through a verb. Its one live use is `Group.require_admin`
        letting that system user clear any group's curation without a membership row.
    created_at: first-seen timestamp.
    """

    # `user` is a reserved SQL word, so the physical table keeps its original name rather than the
    # class-derived `user` an unquoted CREATE TABLE would choke on
    __tablename__ = "users"

    display_name: str | None = Field(default=None, sa_type=Text)
    oidc_subject: str | None = Field(default=None, unique=True, sa_type=Text)
    is_admin: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})

    # no back_populates: Membership carries no `user` relationship of its own, every read
    # site already id-keyed rather than navigating from a loaded Membership. No `groups`
    # association proxy either, the one thing that would have chained off this into `Group` and
    # was itself never called; a caller that wants a user's groups resolves them by id
    # through `Membership.writable_group_ids` or a plain query instead.
    memberships: list[Membership] = Relationship(cascade_delete=True, passive_deletes=True)

    @classmethod
    async def create(cls, display_name: str) -> Self:
        """Create a user row, the multi-user onboarding seam.

        display_name: human-readable label for the new actor.
        """
        user = cls(display_name=display_name)
        session().add(user)
        await session().flush()
        return user

    @classmethod
    async def link_oidc(cls, oidc_subject: str, display_name: str) -> Self:
        """Bind an OIDC subject to a user, minting one on first sight, the row back.

        Provisions the user the human or machine presenting that subject's token acts as, so a
        named user exists before its first login rather than waiting to be auto-created. A regular
        user, never an admin: engine admin standing is the seeded system user alone. Idempotent, a
        second call over the same subject just returns the existing row. Runs under a system
        session since it is a pre-auth bootstrap no user is resolved through yet.

        oidc_subject: the subject claim the provider mints this identity's tokens against.
        display_name: human-readable label for a freshly minted user.
        """
        user = await session().scalar(select(cls).where(cls.oidc_subject == oidc_subject))
        if user is None:
            user = cls(display_name=display_name, oidc_subject=oidc_subject)
            session().add(user)
            await session().flush()
        return user

    @classmethod
    async def administers(cls, user_id: uuid.UUID) -> bool:
        """Whether a user holds engine admin standing, the group-curation override.

        Read by `Group.require_admin` so the engine admin clears any group's review without a
        membership row of its own. Only the seeded system user carries the flag, since engine
        admin is the Postgres owner the CLI acts as rather than an app user promoted through a
        verb, so this reads true for that one identity and false for every other, including an
        unknown one. Reads the is_admin column off the caller's own already-open session; User
        carries no row level security of its own, so any open session reads every row regardless
        of its acting user, and a caller passes in the session it already holds.

        user_id: identity whose admin standing is resolved.
        """
        return bool(await session().scalar(select(cls.is_admin).where(cls.id == user_id)))

    @classmethod
    async def list_all(cls) -> list[Self]:
        """List every user known to the engine in first-seen order, the roster."""
        return list(await session().scalars(select(cls).order_by(cls.created_at)))

    @classmethod
    async def recent_writes(cls, user_id: uuid.UUID, limit: int = 20) -> list[Document]:
        """List the most recent visible document writes with their owner, scope, and promotion.

        Returns the latest documents under the caller's row level security visibility, newest
        first, so an audit reads who wrote what, into which scope, and whether the row was
        promoted from another document, the provenance `promoted_from` link records. The caller
        holds only the id here, resolved from an MCP string argument rather than a loaded
        `User`, so this stays id-keyed and opens its own user-scoped session.

        user_id: identity whose visibility scopes the audit listing.
        limit: maximum number of documents to return.
        """
        async with acting_as(user_id):
            return list(
                await session().scalars(
                    select(Document).order_by(Document.created_at.desc()).limit(limit)
                )
            )

    @classmethod
    async def for_subject(cls, subject: str) -> uuid.UUID:
        """Map a OIDC subject to its aizk user, provisioning one on first sight.

        Looks up the user whose oidc_subject matches and returns its id, and when none exists
        yet creates one stamped with the subject so a OIDC user is provisioned on first
        authenticated call and stays stable across calls after. Runs as the system user since
        no aizk user is known until this resolves one, and the token-verification call site
        holds no session of its own to reuse.

        subject: the OIDC subject claim naming the external user.
        """
        async with as_system():
            user_id = await session().scalar(select(cls.id).where(cls.oidc_subject == subject))
            if user_id is not None:
                return user_id
            user = cls(oidc_subject=subject)
            session().add(user)
            await session().flush()
            return user.id

    @classmethod
    @functools.cache
    def cached_verifier(
        cls,
        issuer: str,
        jwks_uri: str,
        introspect_url: str,
        client_id: str,
        client_secret: str,
        algorithm: str,
        required_scopes: str,
    ) -> TokenVerifier | None:
        """Build the verifier for one set of OIDC settings, memoized so repeat settings reuse
        it.

        Cached on the primitive settings values rather than the unhashable `Settings` object, so a
        test that monkeypatches the OIDC fields builds its own verifier without disturbing the
        one already cached for the process's real configuration. An empty issuer means auth is
        off. An introspection url routes tokens through the live RFC 7662 round-trip, which also
        catches a token revoked before expiry, falling back to the offline JWKS check with no
        per-call network trip when absent. `verifier` is the entrypoint that forwards the live
        settings here. Call that instead unless a test needs to pin a specific settings tuple.

        issuer: base issuer URL whose tokens are accepted, empty to leave the OIDC path off.
        jwks_uri: JWKS endpoint the offline signature path fetches keys from.
        introspect_url: RFC 7662 introspection endpoint, empty to prefer the offline JWKS path.
        client_id: resource server client id the introspection call authenticates as.
        client_secret: resource server client secret paired with client_id.
        algorithm: JWS algorithm the issuer signs its tokens with, matched against the token
            header on the offline JWKS path. Providers differ, Logto signs ES384 while many
            others default to RS256, so the wrong value fails every signature silently.
        required_scopes: comma-separated scopes a token must carry, also the `scopes_supported`
            the resource metadata advertises so a client requests exactly them, empty to accept
            any and advertise none.
        """
        if not issuer:
            return None
        scopes = [scope.strip() for scope in required_scopes.split(",") if scope.strip()] or None
        if introspect_url:
            return IntrospectionTokenVerifier(
                introspection_url=introspect_url,
                client_id=client_id,
                client_secret=client_secret,
                required_scopes=scopes,
            )
        return JWTVerifier(
            jwks_uri=jwks_uri, issuer=issuer, algorithm=algorithm, required_scopes=scopes
        )

    @classmethod
    def verifier(cls) -> TokenVerifier | None:
        """Return the process-cached token verifier for the currently configured OIDC settings.

        None when `oidc_issuer` is empty, the auth-off default a personal single-user stack
        runs under.
        """
        return cls.cached_verifier(
            settings.oidc_issuer,
            settings.oidc_jwks_url,
            settings.oidc_introspect_url,
            settings.oidc_client_id,
            settings.oidc_client_secret,
            settings.oidc_algorithm,
            settings.oidc_required_scopes,
        )

    @classmethod
    def auth_provider(cls) -> AuthProvider | None:
        """The MCP server's auth: verify tokens, and advertise the issuer when a URL is set.

        Wrapping the token verifier in a `RemoteAuthProvider` publishes the RFC 9728 protected
        resource metadata that names the OIDC issuer as this server's authorization server, so a
        client that hits the endpoint unauthenticated is told where to log in and then obtains and
        refreshes its own tokens through the identity provider, no key to mint or paste. With no
        `mcp_resource_url` to advertise from, the bare verifier is served instead, the single-user
        path where the caller already presents a token, and none at all leaves auth off.
        """
        verifier = cls.verifier()
        if verifier is None or not settings.mcp_resource_url:
            return verifier
        return RemoteAuthProvider(
            token_verifier=verifier,
            authorization_servers=[AnyHttpUrl(settings.oidc_issuer)],
            base_url=settings.mcp_resource_url,
            resource_name="aizk",
        )

    @classmethod
    async def from_token(cls, token: str) -> uuid.UUID | None:
        """Validate a OIDC bearer token and resolve it to an aizk user, null when invalid.

        Verifies the token through the configured verifier, introspection or the offline JWKS
        check, and on a valid token maps its `sub` claim to a user, provisioning one on first
        sight. An invalid, unverifiable, or unauthenticated (no verifier configured) token resolves
        to null so it authenticates no one and the caller falls through to the next auth source,
        and `is_admin` stays governed by the user row aizk owns rather than any claim the
        token carries. When the provider injects the configured `oidc_groups_claim`, its list of
        `{id, role, name}` organizations drives `Group.sync_user_groups`, so the caller's group
        memberships follow the identity provider on every authenticated request rather than a
        hand-run `add-member`. The token verification runs outside any session, and `for_subject`
        opens its own only once a claim is actually ready to resolve, so a slow network round trip
        to OIDC never holds one open for nothing.

        token: the raw bearer token presented by the caller.
        """
        active_verifier = cls.verifier()
        if active_verifier is None:
            return None
        access_token = await active_verifier.verify_token(token)
        if access_token is None:
            return None
        subject = access_token.claims.get("sub")
        if not isinstance(subject, str):
            return None
        user_id = await cls.for_subject(subject)
        claim = access_token.claims.get(settings.oidc_groups_claim)
        if settings.oidc_groups_claim and isinstance(claim, list):
            from .group import Group  # local import breaks the User<->Group definition cycle

            async with as_system():
                await Group.sync_user_groups(user_id, claim)
        return user_id
