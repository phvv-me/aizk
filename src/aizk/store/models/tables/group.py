import uuid
from datetime import UTC, datetime
from typing import Self

from loguru import logger
from sqlalchemy import Text, delete, false, func, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import Field

from ....config import settings
from ....exceptions import NotGroupAdminError, ScopeNotFoundError
from ...engine import session
from ...mixins import Id, TableBase
from ..views.live_fact import LiveFact
from .fact import FactClaim
from .membership import Membership
from .user import User


class Group(Id, TableBase, table=True):
    """A sharing scope a user can belong to, the unit rows are scoped against.

    Maps to the `group_` table, since GROUP is a reserved SQL keyword, and
    `TableBase.__tablename__` suffixes the auto-derived name with `_` on any such collision rather
    than needing a manual pin.

    id: stable identity, generated client-side on insert.
    name: unique human-readable label the tools resolve a scope by.
    public: whether the group's rows are readable by anyone, member or not, the shared-brain
        publishing switch. Writing always requires an explicit writer or admin membership.
    curated: whether a write into this group's canon must clear group-admin review before it
        becomes visible to anyone but its author, the review loop that keeps the group's floor to
        verified knowledge. Writing itself still only needs a writer or admin membership same as
        any group, curation gates visibility of the write, not the right to attempt it.
    oidc_org_id: the Logto organization this group is the local projection of, when membership is
        sourced from the identity provider rather than hand-managed. Null for a purely local group.
        Unique so one local group stands for one organization; Postgres treats nulls as distinct,
        so any number of local-only groups coexist.
    """

    name: str = Field(sa_type=Text, unique=True)
    public: bool = Field(default=False, sa_column_kwargs={"server_default": false()})
    curated: bool = Field(default=False, sa_column_kwargs={"server_default": false()})
    oidc_org_id: str | None = Field(default=None, sa_type=Text, unique=True)

    @classmethod
    async def create(
        cls,
        name: str,
        public: bool = False,
        curated: bool = False,
        creator: uuid.UUID | None = None,
    ) -> Self:
        """Create a sharing group, the scope memberships and promotions target.

        The creator, when named, joins the group as its admin member in the same transaction, so
        whoever mints a group can immediately write into it and review its pending canon rather
        than being locked out of their own scope until a separate `add_member` call.

        name: unique human-readable label for the group.
        public: whether the group's rows are readable by anyone from the start.
        curated: whether a write into this group's canon must clear group-admin review before it
            becomes visible to the rest of the group, immediate when false.
        creator: user that founds the group, enrolled as its admin member, none to create an
            ownerless group whose members are all added explicitly.
        """
        group = cls(name=name, public=public, curated=curated)
        session().add(group)
        await session().flush()
        if creator is not None:
            session().add(
                Membership(user_id=creator, group_id=group.id, role=Membership.Role.admin)
            )
        return group

    @classmethod
    async def named(cls, name: str) -> Self:
        """Resolve a group by name, raising when no such group exists.

        name: unique group name to resolve.
        """
        group = await session().scalar(select(cls).where(cls.name == name))
        if group is None:
            raise ScopeNotFoundError(f"no scope named {name!r}")
        return group

    @classmethod
    async def for_oidc_org(cls, oidc_org_id: str, name: str) -> Self:
        """Resolve the local group mirroring a Logto organization, minting it on first sight.

        The bridge that lets Logto own membership while aizk keeps the scope lattice: a row's
        `scopes uuid[]` still references local group uuids, and this maps a token's Logto
        organization id onto the one local group that stands for it, creating it the first time a
        member of that organization is seen. Idempotent on the organization id's unique constraint.

        oidc_org_id: the Logto organization id this group is the local projection of.
        name: label to give the group when first minted, ignored once the mirror exists. The
            organization id itself is appended when the bare label is already taken, since a group
            name is unique but two organizations may share a display name.
        """
        group = await session().scalar(select(cls).where(cls.oidc_org_id == oidc_org_id))
        if group is not None:
            return group
        taken = await session().scalar(select(cls.id).where(cls.name == name))
        group = cls(name=f"{name} ({oidc_org_id})" if taken else name, oidc_org_id=oidc_org_id)
        session().add(group)
        await session().flush()
        return group

    @classmethod
    async def sync_user_groups(cls, user_id: uuid.UUID, memberships: list[dict[str, str]]) -> None:
        """Reconcile a user's group memberships to exactly what a verified token claims.

        The token is the source of truth for who belongs where. Each entry names a Logto
        organization, the role the user holds in it, and a label; every named organization is
        mirrored to its local group, the user's membership is upserted to the claimed role, and any
        membership the token no longer claims is dropped, so a user removed from an organization in
        Logto loses that scope on their next authenticated request. The scope lattice and row level
        security are untouched, only the membership rows they already read are now driven by Logto
        rather than a hand-run `add_member`. Runs under the system role, since it writes the
        non-scoped `membership` table on the caller's behalf.

        user_id: the aizk user the token resolved to.
        memberships: the token's claim, each entry `{"id": <logto org id>, "role": <role>,
            "name": <label>}`; an empty list drops every membership the user held.
        """
        desired: dict[uuid.UUID, Membership.Role] = {}
        for entry in memberships:
            # a hostile or drifted claim (a bare string, a dict without `id`, a role outside the
            # enum) must never crash auth: one bad entry would otherwise fail every request the
            # token makes and lock out a whole org. Skip the entry, keep reconciling the rest.
            try:
                org_id = entry["id"]
                role = Membership.Role(entry.get("role", Membership.Role.reader))
                name = entry.get("name", org_id)
            except TypeError, KeyError, ValueError, AttributeError:
                logger.warning("skipping malformed group claim entry {!r}", entry)
                continue
            if not isinstance(org_id, str):
                logger.warning("skipping group claim entry with non-string id {!r}", entry)
                continue
            group = await cls.for_oidc_org(org_id, name)
            desired[group.id] = role
        # reconcile only the Logto-backed memberships; a hand-managed local group carries no
        # oidc_org_id and is never dropped just because a token happens not to mention it
        oidc_backed = select(cls.id).where(cls.oidc_org_id.is_not(None))
        await session().execute(
            delete(Membership).where(
                Membership.user_id == user_id,
                Membership.group_id.in_(oidc_backed),
                Membership.group_id.not_in(desired) if desired else true(),
            )
        )
        for group_id, role in desired.items():
            await session().execute(
                pg_insert(Membership)
                .values(user_id=user_id, group_id=group_id, role=role)
                .on_conflict_do_update(index_elements=["user_id", "group_id"], set_={"role": role})
            )

    @classmethod
    async def list_all(cls) -> list[dict[str, str | bool | int]]:
        """List every group with its visibility and member count, the admin roster view."""
        counted = (
            select(cls.name, cls.public, func.count(Membership.user_id).label("members"))
            .outerjoin(Membership, Membership.group_id == cls.id)
            .group_by(cls.name, cls.public)
            .order_by(cls.name)
        )
        rows = (await session().execute(counted)).all()
        return [
            {"name": name, "public": public, "members": members} for name, public, members in rows
        ]

    async def add_member(self, user_id: uuid.UUID, role: str = "writer") -> None:
        """Add a user to this group so its scope becomes visible under row security.

        user_id: user joining the group.
        role: standing within the group, reader for read-only visibility, writer or admin to also
            write into the shared scope.
        """
        session().add(Membership(user_id=user_id, group_id=self.id, role=role))

    async def remove_member(self, user_id: uuid.UUID) -> None:
        """Remove a user from this group, so its scope stops being visible to them.

        Rows the user wrote into the scope stay with the group, owned but no longer reachable
        by the departed member, mirroring how a team keeps a leaver's contributions.

        user_id: user leaving the group.
        """
        await session().execute(
            delete(Membership)
            .where(Membership.user_id == user_id)
            .where(Membership.group_id == self.id)
        )

    async def admin(self, user_id: uuid.UUID) -> bool:
        """Whether a user holds the admin membership role in this group.

        user_id: identity whose standing in the group is checked.
        """
        role = await session().scalar(
            select(Membership.role).where(
                Membership.user_id == user_id, Membership.group_id == self.id
            )
        )
        return role == Membership.Role.admin

    async def require_admin(self, user_id: uuid.UUID) -> None:
        """Refuse a call unless the user administers this group or the whole engine.

        Standing comes from holding this group's own admin membership role, or from the
        server-wide `User.administers` flag, so a group's own admins and an engine admin can
        both work its curation queue, the gate every curation tool runs its body through before it
        ever reads or writes a fact.

        user_id: caller whose standing is checked.
        """
        if await User.administers(user_id) or await self.admin(user_id):
            return
        raise NotGroupAdminError(f"{user_id} does not administer group {self.id}")

    async def publish(self, public: bool = True) -> None:
        """Flip this group's public read flag, the shared-brain publishing switch.

        A public group's rows are readable by any caller, member or not, anonymous included, while
        writing keeps requiring an explicit writer or admin membership.

        public: the new visibility, true to publish and false to make members-only again.
        """
        self.public = public
        session().add(self)

    async def curate(self, curated: bool = True) -> None:
        """Flip this group's curation flag, the shared-brain review-gate switch.

        A curated group's writes land pending until a group admin approves them through
        `approve_facts`, while an uncurated group keeps writing straight into the visible canon.

        curated: the new curation state.
        """
        self.curated = curated
        session().add(self)

    async def demote_scoped_rows(self) -> None:
        """Drop colliding claims, then demote every scoped row naming this group back to private.

        A `uuid[]` scope-set column carries no foreign key, Postgres has no such constraint on an
        array element, so nothing cascades on its own when a group is deleted. This method is the
        explicit demotion `ON DELETE SET NULL` gave a singleton `scope` column for free. It
        widens, never narrows, an id containing group B out of a set never becomes `{A}`, the
        whole set resets to `{}` together, so `{A, B}` demotes to fully private rather than
        silently collapsing to A's own scope alone. The claim dedup runs first since
        `entity_claim`/`fact_claim`'s own uniqueness treats every private claim on the same
        content by the same owner as one identity, so an owner who already privately claims a node
        and also claimed it inside this group would collide the moment both land on the same empty
        set. The redundant about-to-be-demoted claim is simply the one to drop first, since the
        owner already privately holds the same content. Both passes run on the owner-role admin
        connection rather than the ordinary app session, since they must reach every owner's rows,
        not only the caller's own visible slice.
        """
        from sqlalchemy import exists
        from sqlalchemy.orm import aliased

        from .chunk import Chunk
        from .community import Community
        from .document import Document
        from .entity import EntityClaim
        from .profile import Profile
        from .session_item import SessionItem
        from .watermark import Watermark

        scoped = (
            Document,
            Chunk,
            EntityClaim,
            FactClaim,
            Community,
            Profile,
            SessionItem,
            Watermark,
        )
        engine = create_async_engine(settings.admin_database_url)
        try:
            async with engine.begin() as connection:
                for claim in (EntityClaim, FactClaim):
                    held = aliased(claim)  # a private claim the same owner already holds
                    collision = select(held.content_id).where(
                        func.cardinality(held.scopes) == 0,
                        held.owner_id == claim.owner_id,
                        held.content_id == claim.content_id,
                    )
                    predicates = [claim.scopes.contains([self.id])]
                    if claim is FactClaim:  # its partial unique index governs only live rows
                        collision = collision.where(func.upper_inf(held.recorded))
                        predicates.append(func.upper_inf(claim.recorded))
                    await connection.execute(delete(claim).where(*predicates, exists(collision)))
                for model in scoped:
                    await connection.execute(
                        update(model).where(model.scopes.contains([self.id])).values(scopes=[])
                    )
        finally:
            await engine.dispose()

    async def delete(self) -> None:
        """Delete this group, its memberships cascading and its rows falling back to private.

        `demote_scoped_rows` is what makes the group's shared rows fall back to their owners'
        private scope rather than left naming a group that no longer exists, since a scope-set
        array carries no foreign key for Postgres to cascade through on its own the way a
        singleton `scope` column once did.
        """
        await self.demote_scoped_rows()
        await session().delete(self)

    @classmethod
    async def review_stamp(
        cls, scopes: tuple[uuid.UUID, ...], owner_id: uuid.UUID
    ) -> datetime | None:
        """The reviewed_at a new claim in this scope set, written by this owner, should carry.

        Private (empty) and a set naming no curated group stamp immediately, the unchanged
        single-user and ordinary-sharing behavior. A set naming at least one curated group stamps
        immediately only when the owner already holds the admin membership role in every curated
        group the set names, otherwise the claim lands pending, invisible to everyone but its
        author until a group admin approves it through `approve_facts`.

        scopes: the group set the new claim is written into, private when empty.
        owner_id: user writing the claim, whose admin standing in every curated group decides.
        """
        if not scopes:
            return datetime.now(UTC)
        curated_ids = set(
            await session().scalars(select(cls.id).where(cls.id.in_(scopes), cls.curated))
        )
        if not curated_ids:
            return datetime.now(UTC)
        admin_ids = set(
            await session().scalars(
                select(Membership.group_id).where(
                    Membership.user_id == owner_id,
                    Membership.group_id.in_(curated_ids),
                    Membership.role == Membership.Role.admin,
                )
            )
        )
        return datetime.now(UTC) if curated_ids <= admin_ids else None

    async def pending_facts(self) -> list[LiveFact]:
        """The unreviewed live claims touching this curated group, the group admin's review queue.

        Runs under a session already acting as the system user, whose server-wide admin
        standing the curation-admin row level security policy always lets through for any curated
        group's rows regardless of local membership, so the read reaches every member's pending
        claim rather than only its own author's. `scopes.contains([self.id])` matches any claim
        whose scope set names this group at all, a bridge claim spanning this group and another
        included, since this group's own review still governs its slice of that claim. Reading
        `LiveFact` rather than `FactClaim` narrows to the current version of each statement, the
        `live_fact` view already carrying that predicate, and since it is a distinct mapped class
        the `do_orm_execute` listener's `FactClaim`-keyed loader criteria never attaches, so a
        claim still pending review from another author surfaces here too.
        """
        return list(
            await session().scalars(
                select(LiveFact)
                .where(LiveFact.scopes.contains([self.id]), LiveFact.reviewed_at.is_(None))
                .order_by(LiveFact.recorded)
            )
        )

    async def approve_facts(self, fact_ids: list[uuid.UUID] | None = None) -> int:
        """Stamp reviewed_at=now() on this curated group's pending claims, return how many changed.

        Runs under a session already acting as the system user, which the curation-admin row
        level security policy always lets through for a curated group, so the write succeeds
        regardless of the calling group or server admin's own membership. `require_admin` is the
        gate that already vetted them before this ever runs.

        fact_ids: claim ids to approve, every still-pending claim touching the group when null.
        """
        statement = update(FactClaim).where(
            FactClaim.scopes.contains([self.id]), FactClaim.reviewed_at.is_(None)
        )
        if fact_ids is not None:
            statement = statement.where(FactClaim.id.in_(fact_ids))
        result = await session().execute(statement.values(reviewed_at=datetime.now(UTC)))
        return result.rowcount or 0

    async def reject_facts(self, fact_ids: list[uuid.UUID]) -> int:
        """Delete this curated group's named pending claims, return how many were removed.

        A rejected claim never became canonical, so it is deleted outright rather than merely
        hidden, running under the same curation-admin session reach `approve_facts` relies on. The
        fact content it staked, if any other container's claim still references it, is untouched,
        immutable and shared beneath whichever claims remain.

        fact_ids: claim ids to reject.
        """
        result = await session().execute(
            delete(FactClaim).where(
                FactClaim.scopes.contains([self.id]),
                FactClaim.reviewed_at.is_(None),
                FactClaim.id.in_(fact_ids),
            )
        )
        return result.rowcount or 0
