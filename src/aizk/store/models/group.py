import uuid
from datetime import UTC, datetime
from typing import Self

from sqlalchemy import Text, delete, false, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlmodel import Field

from ...config import settings
from ...exceptions import NotGroupAdminError, ScopeNotFoundError
from ..mixins import Id, TableBase
from .fact import FactClaim
from .live_fact import LiveFact
from .membership import Membership
from .principal import Principal

# claim tables whose (content_id, owner_id, scope) uniqueness, NULLS NOT DISTINCT, can collide once
# a deleted group's ON DELETE SET NULL demotes a group-scoped claim onto the same owner's already-
# private claim on the identical content; fact_claim's own extra predicate keeps the check to its
# live rows, the only ones its own partial unique index governs.
CLAIM_DEDUPE_STATEMENTS = (
    "DELETE FROM entity_claim demoted USING entity_claim private "
    "WHERE demoted.scope = :group_id AND private.scope IS NULL "
    "AND private.owner_id = demoted.owner_id AND private.content_id = demoted.content_id",
    "DELETE FROM fact_claim demoted USING fact_claim private "
    "WHERE demoted.scope = :group_id AND private.scope IS NULL "
    "AND private.owner_id = demoted.owner_id AND private.content_id = demoted.content_id "
    "AND upper_inf(demoted.recorded) AND upper_inf(private.recorded)",
)


class Group(Id, TableBase, table=True):
    """A sharing scope a principal can belong to, the unit rows are scoped against.

    Maps to the `group_` table: GROUP is a reserved SQL keyword, and `TableBase.__tablename__`
    suffixes the auto-derived name with `_` on any such collision rather than needing a manual pin.

    id: stable identity, generated client-side on insert.
    name: unique human-readable label the tools resolve a scope by.
    public: whether the group's rows are readable by anyone, member or not, the shared-brain
        publishing switch. Writing always requires an explicit writer or admin membership.
    curated: whether a write into this group's canon must clear group-admin review before it
        becomes visible to anyone but its author, the review loop that keeps the group's floor to
        verified knowledge. Writing itself still only needs a writer or admin membership same as
        any group, curation gates visibility of the write, not the right to attempt it.
    """

    name: str = Field(sa_type=Text, sa_column_kwargs={"unique": True})
    public: bool = Field(default=False, sa_column_kwargs={"server_default": false()})
    curated: bool = Field(default=False, sa_column_kwargs={"server_default": false()})

    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        name: str,
        public: bool = False,
        curated: bool = False,
        creator: uuid.UUID | None = None,
    ) -> Self:
        """Create a sharing group, the scope memberships and promotions target.

        The creator, when named, joins the group as its admin member in the same transaction, so
        whoever mints a group can immediately write into it and review its pending canon rather
        than being locked out of their own scope until a separate `add_member` call.

        session: open session the group and its founding membership are written through.
        name: unique human-readable label for the group.
        public: whether the group's rows are readable by anyone from the start.
        curated: whether a write into this group's canon must clear group-admin review before it
            becomes visible to the rest of the group, immediate when false.
        creator: principal that founds the group, enrolled as its admin member, none to create an
            ownerless group whose members are all added explicitly.
        """
        group = cls(name=name, public=public, curated=curated)
        session.add(group)
        await session.flush()
        if creator is not None:
            session.add(
                Membership(principal_id=creator, group_id=group.id, role=Membership.Role.admin)
            )
        return group

    @classmethod
    async def named(cls, session: AsyncSession, name: str) -> Self:
        """Resolve a group by name, raising when no such group exists.

        session: open session the group is read through.
        name: unique group name to resolve.
        """
        group = await session.scalar(select(cls).where(cls.name == name))
        if group is None:
            raise ScopeNotFoundError(f"no scope named {name!r}")
        return group

    @classmethod
    async def list_all(cls, session: AsyncSession) -> list[dict[str, str | bool | int]]:
        """List every group with its visibility and member count, the admin roster view.

        session: open session the roster is read through.
        """
        counted = (
            select(cls.name, cls.public, func.count(Membership.principal_id).label("members"))
            .outerjoin(Membership, Membership.group_id == cls.id)
            .group_by(cls.name, cls.public)
            .order_by(cls.name)
        )
        rows = (await session.execute(counted)).all()
        return [
            {"name": name, "public": public, "members": members} for name, public, members in rows
        ]

    async def add_member(
        self, session: AsyncSession, principal_id: uuid.UUID, role: str = "writer"
    ) -> None:
        """Add a principal to this group so its scope becomes visible under row security.

        session: open session the membership is written through.
        principal_id: principal joining the group.
        role: standing within the group, reader for read-only visibility, writer or admin to also
            write into the shared scope.
        """
        session.add(Membership(principal_id=principal_id, group_id=self.id, role=role))

    async def remove_member(self, session: AsyncSession, principal_id: uuid.UUID) -> None:
        """Remove a principal from this group, so its scope stops being visible to them.

        Rows the principal wrote into the scope stay with the group, owned but no longer reachable
        by the departed member, mirroring how a team keeps a leaver's contributions.

        session: open session the membership is removed through.
        principal_id: principal leaving the group.
        """
        await session.execute(
            delete(Membership)
            .where(Membership.principal_id == principal_id)
            .where(Membership.group_id == self.id)
        )

    async def admin(self, session: AsyncSession, principal_id: uuid.UUID) -> bool:
        """Whether a principal holds the admin membership role in this group.

        session: open session the membership is read through.
        principal_id: identity whose standing in the group is checked.
        """
        role = await session.scalar(
            select(Membership.role).where(
                Membership.principal_id == principal_id, Membership.group_id == self.id
            )
        )
        return role == Membership.Role.admin

    async def require_admin(self, session: AsyncSession, principal_id: uuid.UUID) -> None:
        """Refuse a call unless the principal administers this group or the whole engine.

        Standing comes from holding this group's own admin membership role, or from the
        server-wide `Principal.administers` flag, so a group's own admins and an engine admin can
        both work its curation queue, the gate every curation tool runs its body through before it
        ever reads or writes a fact.

        session: open session the membership is read through.
        principal_id: caller whose standing is checked.
        """
        if await Principal.administers(principal_id) or await self.admin(session, principal_id):
            return
        raise NotGroupAdminError(f"{principal_id} does not administer group {self.id}")

    async def publish(self, session: AsyncSession, public: bool = True) -> None:
        """Flip this group's public read flag, the shared-brain publishing switch.

        A public group's rows are readable by any caller, member or not, anonymous included, while
        writing keeps requiring an explicit writer or admin membership.

        session: open session the flag is written through.
        public: the new visibility, true to publish and false to make members-only again.
        """
        self.public = public
        session.add(self)

    async def curate(self, session: AsyncSession, curated: bool = True) -> None:
        """Flip this group's curation flag, the shared-brain review-gate switch.

        A curated group's writes land pending until a group admin approves them through
        `approve_facts`, while an uncurated group keeps writing straight into the visible canon.

        session: open session the flag is written through.
        curated: the new curation state.
        """
        self.curated = curated
        session.add(self)

    async def drop_colliding_claims(self) -> None:
        """Delete a group-scoped claim that would collide with its owner's own private claim.

        `ON DELETE SET NULL` demotes every claim this group's scope touches to private the moment
        it is deleted, but `entity_claim`/`fact_claim`'s own uniqueness treats every private claim
        on the same content by the same owner as one identity, so an owner who already privately
        claims a node and also claimed it inside this group would collide the moment both land on
        the same null scope. The redundant group-scoped claim is simply the one to drop, since the
        owner already privately holds the same content. This runs on the owner-role admin
        connection rather than the ordinary app session, since it must reach every owner's claims,
        not only the caller's own visible slice: referential integrity checks (the `ON DELETE SET
        NULL` cascade itself and its own unique re-check) bypass row level security by Postgres's
        own design, so the demotion was never the gap; only this proactive dedup, an ordinary
        DELETE and thus RLS-governed like any other, needs the elevated connection to reach a claim
        its own owner alone could otherwise see.
        """
        admin = create_async_engine(settings.admin_database_url)
        try:
            async with admin.begin() as connection:
                for statement in CLAIM_DEDUPE_STATEMENTS:
                    await connection.execute(text(statement), {"group_id": self.id})
        finally:
            await admin.dispose()

    async def delete(self, session: AsyncSession) -> None:
        """Delete this group, its memberships cascading and its rows falling back to private.

        Every scope column references `group_` with ON DELETE SET NULL, so the group's shared rows
        are not lost but demoted to their owners' private scope; a claim whose owner already
        privately holds the same content is dropped first instead, so the demotion never collides
        with itself.

        session: open session the group is deleted through.
        """
        await self.drop_colliding_claims()
        await session.delete(self)

    @classmethod
    async def review_stamp(
        cls, session: AsyncSession, scope: uuid.UUID | None, owner_id: uuid.UUID
    ) -> datetime | None:
        """The reviewed_at a new claim in this scope, written by this owner, should carry.

        Private scope and an uncurated group stamp immediately, the unchanged single-user and
        ordinary-sharing behavior. A curated group stamps immediately only when the owner already
        holds its admin membership role, otherwise the claim lands pending, invisible to everyone
        but its author until a group admin approves it through `approve_facts`.

        session: open session the scope and membership are read from, neither table row-level
            secured so any principal-scoped session reads both regardless of the acting principal.
        scope: group the new claim is written into, private when null.
        owner_id: principal writing the claim, whose admin standing in a curated scope decides.
        """
        if scope is None:
            return datetime.now(UTC)
        curated = await session.scalar(select(cls.curated).where(cls.id == scope))
        if not curated:
            return datetime.now(UTC)
        role = await session.scalar(
            select(Membership.role).where(
                Membership.principal_id == owner_id, Membership.group_id == scope
            )
        )
        return datetime.now(UTC) if role == Membership.Role.admin else None

    async def pending_facts(self, session: AsyncSession) -> list[LiveFact]:
        """The unreviewed live claims of this curated group, the group admin's review queue.

        Runs under a session already acting as the system principal, whose server-wide admin
        standing the curation-admin row level security policy always lets through for any curated
        group's rows regardless of local membership, so the read reaches every member's pending
        claim rather than only its own author's. Reading `LiveFact` rather than `FactClaim`
        narrows to the current version of each statement, the `live_fact` view already carrying
        that predicate, and since it is a distinct mapped class the `do_orm_execute` listener's
        `FactClaim`-keyed loader criteria never attaches, so a claim still pending review from
        another author surfaces here too.

        session: open session, already acting as the system principal.
        """
        return list(
            await session.scalars(
                select(LiveFact)
                .where(LiveFact.scope == self.id, LiveFact.reviewed_at.is_(None))
                .order_by(LiveFact.recorded)
            )
        )

    async def approve_facts(
        self, session: AsyncSession, fact_ids: list[uuid.UUID] | None = None
    ) -> int:
        """Stamp reviewed_at=now() on this curated group's pending claims, return how many changed.

        Runs under a session already acting as the system principal, which the curation-admin row
        level security policy always lets through for a curated group, so the write succeeds
        regardless of the calling group or server admin's own membership; `require_admin` is the
        gate that already vetted them before this ever runs.

        session: open session, already acting as the system principal.
        fact_ids: claim ids to approve, every still-pending claim in the group when null.
        """
        statement = update(FactClaim).where(
            FactClaim.scope == self.id, FactClaim.reviewed_at.is_(None)
        )
        if fact_ids is not None:
            statement = statement.where(FactClaim.id.in_(fact_ids))
        result = await session.execute(statement.values(reviewed_at=datetime.now(UTC)))
        return result.rowcount or 0

    async def reject_facts(self, session: AsyncSession, fact_ids: list[uuid.UUID]) -> int:
        """Delete this curated group's named pending claims, return how many were removed.

        A rejected claim never became canonical, so it is deleted outright rather than merely
        hidden, running under the same curation-admin session reach `approve_facts` relies on. The
        fact content it staked, if any other container's claim still references it, is untouched,
        immutable and shared beneath whichever claims remain.

        session: open session, already acting as the system principal.
        fact_ids: claim ids to reject.
        """
        result = await session.execute(
            delete(FactClaim).where(
                FactClaim.scope == self.id,
                FactClaim.reviewed_at.is_(None),
                FactClaim.id.in_(fact_ids),
            )
        )
        return result.rowcount or 0
