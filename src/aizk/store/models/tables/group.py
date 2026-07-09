import uuid
from typing import Self

from sqlalchemy import Text, delete, exists, false, func, select, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import aliased
from sqlmodel import Field

from ....config import settings
from ....exceptions import ScopeNotFoundError
from ...engine import session
from ...mixins import Id, TableBase
from .fact import FactClaim
from .membership import Membership


class Group(Id, TableBase, table=True):
    """A sharing scope a user can belong to, the unit rows are scoped against.

    Maps to the `group_` table, since GROUP is a reserved SQL keyword, and
    `TableBase.__tablename__` suffixes the auto-derived name with `_` on any such collision rather
    than needing a manual pin.

    id: stable identity, generated client-side on insert.
    name: unique human-readable label the tools resolve a scope by.
    public: whether the group's rows are readable by anyone, member or not, the shared-brain
        publishing switch. Writing always requires an explicit writer or admin membership.
    """

    name: str = Field(sa_type=Text, unique=True)
    public: bool = Field(default=False, sa_column_kwargs={"server_default": false()})

    @classmethod
    async def create(cls, name: str, creator: uuid.UUID, public: bool = False) -> Self:
        """Create a sharing group, enrolling its creator as the admin member.

        The creator joins as admin in the same transaction, so whoever mints a group can
        immediately write into it rather than being locked out of their own scope until a separate
        `add_member`. Requiring a creator means no group is ever born unadministered.

        name: unique human-readable label for the group.
        creator: user that founds the group, enrolled as its admin member.
        public: whether the group's rows are readable by anyone from the start.
        """
        group = cls(name=name, public=public)
        session().add(group)
        await session().flush()
        session().add(Membership(user_id=creator, group_id=group.id, role=Membership.Role.admin))
        return group

    @classmethod
    async def named(cls, name: str) -> Self:
        """Resolve a group by name, raising when no such group exists.

        name: unique group name to resolve.
        """
        if (group := await session().scalar(select(cls).where(cls.name == name))) is None:
            raise ScopeNotFoundError(f"no scope named {name!r}")
        return group

    @classmethod
    async def list_all(cls) -> list[Self]:
        """Every group ordered by name, the admin roster; `count_members` sizes each on demand."""
        return list(await session().scalars(select(cls).order_by(cls.name)))

    async def count_members(self) -> int:
        """How many users hold a membership in this group."""
        total = await session().scalar(
            select(func.count()).select_from(Membership).where(Membership.group_id == self.id)
        )
        return total or 0

    async def add_member(self, user_id: uuid.UUID, role: str = "editor") -> None:
        """Add a user to this group so its scope becomes visible under row security.

        user_id: user joining the group.
        role: standing within the group, viewer for read-only visibility, editor or admin to also
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
            delete(Membership).where(Membership.user_id == user_id, Membership.group_id == self.id)
        )

    async def is_admin(self, user_id: uuid.UUID) -> bool:
        """Whether a user holds the admin membership role in this group.

        user_id: identity whose standing in the group is checked.
        """
        role = await session().scalar(
            select(Membership.role).where(
                Membership.user_id == user_id, Membership.group_id == self.id
            )
        )
        return role == Membership.Role.admin

    async def toggle_public(self) -> None:
        """Flip this group's public read flag, the shared-brain publishing switch.

        A public group's rows are readable by any caller, member or not, anonymous included, while
        writing keeps requiring an explicit writer or admin membership.
        """
        self.public = not self.public
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
