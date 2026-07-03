import uuid
from typing import TYPE_CHECKING, ClassVar, Self

from sqlalchemy import Text, select
from sqlalchemy.ext.associationproxy import AssociationProxy, association_proxy
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field, Relationship

from ..context import acting_as, system_session
from ..mixins import Id, TableBase, Timestamped
from .document import Document

if TYPE_CHECKING:
    from .group import Group
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

    memberships: list[Membership] = Relationship(
        back_populates="principal",
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "passive_deletes": True},
    )
    groups: ClassVar[AssociationProxy[list[Group]]] = association_proxy("memberships", "group")

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
    async def administers(cls, principal_id: uuid.UUID) -> bool:
        """Whether a principal may manage the operational surface, the one admin gate.

        Reads the principal's is_admin column, with an unknown principal reading as false; the
        migration seeds the system principal with the flag already set, so a fresh single-user
        stack self-administers from the first migration with no separate root-principal
        short-circuit. Runs as the system principal since principal is not a scoped table, the
        lookup must see every row, and every caller (the auth middleware, `require_admin`) holds no
        session of its own to reuse at the point it resolves standing.

        principal_id: identity whose administrative standing is resolved.
        """
        async with system_session() as session:
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
