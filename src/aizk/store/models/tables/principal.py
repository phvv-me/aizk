import functools
import uuid
from typing import TYPE_CHECKING, Self

from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.providers.introspection import IntrospectionTokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from sqlalchemy import Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field, Relationship

from ....config import settings
from ...context import acting_as, system_session
from ...mixins import Id, TableBase, Timestamped
from .document import Document

if TYPE_CHECKING:
    from .membership import Membership


class Principal(Id, Timestamped, TableBase, table=True):
    """An actor that can own and read memory, a human or an agent identity.

    id: stable identity, generated client-side on insert.
    display_name: human-readable label when one is known.
    zitadel_sub: unique subject claim from the identity provider, null until linked.
    is_admin: whether this principal manages the operational surface, false for a regular user.
    created_at: first-seen timestamp.
    """

    display_name: str | None = Field(default=None, sa_type=Text)
    zitadel_sub: str | None = Field(default=None, unique=True, sa_type=Text)
    is_admin: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})

    # no back_populates: Membership carries no `principal` relationship of its own, every read
    # site already id-keyed rather than navigating from a loaded Membership. No `groups`
    # association proxy either, the one thing that would have chained off this into `Group` and
    # was itself never called; a caller that wants a principal's groups resolves them by id
    # through `Membership.writable_group_ids` or a plain query instead.
    memberships: list[Membership] = Relationship(cascade_delete=True, passive_deletes=True)

    @classmethod
    async def create(cls, session: AsyncSession, display_name: str) -> Self:
        """Create a principal row, the multi-user onboarding seam.

        session: open session the row is minted through.
        display_name: human-readable label for the new actor.
        """
        principal = cls(display_name=display_name)
        session.add(principal)
        await session.flush()
        return principal

    @classmethod
    async def link_admin(cls, session: AsyncSession, zitadel_sub: str, display_name: str) -> Self:
        """Provision or promote the admin principal a Zitadel subject authenticates as.

        Binds an external identity provider subject to an admin principal so the machine or human
        that presents that subject's token administers the engine, minting one stamped with the
        subject when none carries it yet and promoting the existing one otherwise. Idempotent, a
        second call over the same subject just re-confirms the flag. Runs under a system session
        since it is a pre-auth bootstrap that no principal is resolved to administer through yet.

        session: open system session the row is minted or promoted through.
        zitadel_sub: the subject claim the provider mints this identity's tokens against.
        display_name: human-readable label for a freshly minted principal.
        """
        principal = await session.scalar(select(cls).where(cls.zitadel_sub == zitadel_sub))
        if principal is None:
            principal = cls(display_name=display_name, zitadel_sub=zitadel_sub, is_admin=True)
            session.add(principal)
            await session.flush()
            return principal
        await principal.grant_admin(session)
        return principal

    @classmethod
    async def administers(cls, session: AsyncSession, principal_id: uuid.UUID) -> bool:
        """Whether a principal holds server-wide admin standing, the group-curation override.

        Read by `Group.require_admin` so an engine admin clears any group's review without a
        membership row of its own; the operational surface it once gated on the MCP server has
        moved to the ssh-only CLI, whose operator acts as the already-admin system principal.
        Reads the is_admin column off the caller's own already-open session, an unknown principal
        reading as false. The migration seeds the system principal with the flag already set, so a
        fresh single-user stack self-administers from the first migration. Principal carries no row
        level security of its own, so any open session reads every row regardless of its own acting
        principal, and a caller passes in the session it already holds rather than this method
        nesting a second one inside it.

        session: open session the flag is read through.
        principal_id: identity whose administrative standing is resolved.
        """
        return bool(await session.scalar(select(cls.is_admin).where(cls.id == principal_id)))

    async def grant_admin(self, session: AsyncSession) -> None:
        """Mark this principal as an admin so it manages the operational surface.

        session: open session the update is written through.
        """
        self.is_admin = True
        session.add(self)

    @classmethod
    async def list_all(cls, session: AsyncSession) -> list[Self]:
        """List every principal known to the engine in first-seen order, the admin roster.

        session: open session the roster is read through.
        """
        return list(await session.scalars(select(cls).order_by(cls.created_at)))

    @classmethod
    async def recent_writes(cls, principal_id: uuid.UUID, limit: int = 20) -> list[Document]:
        """List the most recent visible document writes with their owner, scope, and promotion.

        Returns the latest documents under the caller's row level security visibility, newest
        first, so an audit reads who wrote what, into which scope, and whether the row was
        promoted from another document, the provenance `promoted_from` link records. The caller
        holds only the id here, resolved from an MCP string argument rather than a loaded
        `Principal`, so this stays id-keyed and opens its own principal-scoped session.

        principal_id: identity whose visibility scopes the audit listing.
        limit: maximum number of documents to return.
        """
        async with acting_as(principal_id) as session:
            return list(
                await session.scalars(
                    select(Document).order_by(Document.created_at.desc()).limit(limit)
                )
            )

    @classmethod
    async def for_subject(cls, subject: str) -> uuid.UUID:
        """Map a Zitadel subject to its aizk principal, provisioning one on first sight.

        Looks up the principal whose zitadel_sub matches and returns its id, and when none exists
        yet creates one stamped with the subject so a Zitadel user is provisioned on first
        authenticated call and stays stable across calls after. Runs as the system principal since
        no aizk principal is known until this resolves one, and the token-verification call site
        holds no session of its own to reuse.

        subject: the Zitadel subject claim naming the external user.
        """
        async with system_session() as session:
            principal_id = await session.scalar(select(cls.id).where(cls.zitadel_sub == subject))
            if principal_id is not None:
                return principal_id
            principal = cls(zitadel_sub=subject)
            session.add(principal)
            await session.flush()
            return principal.id

    @classmethod
    @functools.cache
    def cached_verifier(
        cls, issuer: str, jwks_uri: str, introspect_url: str, client_id: str, client_secret: str
    ) -> TokenVerifier | None:
        """Build the verifier for one set of Zitadel settings, memoized so repeat settings reuse
        it.

        Cached on the primitive settings values rather than the unhashable `Settings` object, so a
        test that monkeypatches the Zitadel fields builds its own verifier without disturbing the
        one already cached for the process's real configuration. An empty issuer means auth is
        off. An introspection url routes tokens through the live RFC 7662 round-trip, which also
        catches a token revoked before expiry, falling back to the offline JWKS check with no
        per-call network trip when absent. `verifier` is the entrypoint that forwards the live
        settings here. Call that instead unless a test needs to pin a specific settings tuple.

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

    @classmethod
    def verifier(cls) -> TokenVerifier | None:
        """Return the process-cached token verifier for the currently configured Zitadel settings.

        None when `zitadel_issuer` is empty, the auth-off default a personal single-user stack
        runs under.
        """
        return cls.cached_verifier(
            settings.zitadel_issuer,
            settings.zitadel_jwks_url,
            settings.zitadel_introspect_url,
            settings.zitadel_client_id,
            settings.zitadel_client_secret,
        )

    @classmethod
    async def from_token(cls, token: str) -> uuid.UUID | None:
        """Validate a Zitadel bearer token and resolve it to an aizk principal, null when invalid.

        Verifies the token through the configured verifier, introspection or the offline JWKS
        check, and on a valid token maps its `sub` claim to a principal, provisioning one on first
        sight. An invalid, unverifiable, or unauthenticated (no verifier configured) token resolves
        to null so it authenticates no one and the caller falls through to the next auth source,
        and `is_admin` stays governed by the principal row aizk owns rather than any claim the
        token carries. The token verification runs outside any session, and `for_subject` opens its
        own only once a claim is actually ready to resolve, so a slow network round trip to Zitadel
        never holds one open for nothing.

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
        return await cls.for_subject(subject)
