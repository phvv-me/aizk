import uuid
from typing import Self

from sqlalchemy import Text, delete, false, func, select
from sqlmodel import Field

from ....exceptions import ScopeNotFoundError
from ...engine import session
from ...mixins import Id, TableBase
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
    oidc_org_id: the Logto organization this group is the local projection of, the sole way a group
        comes to exist. Every group mirrors one organization, minted by `User.sync_groups` on first
        sight of a member's token, never hand-created, so this is required and unique rather than a
        nullable marker of a local-only group.
    """

    name: str = Field(sa_type=Text, unique=True)
    public: bool = Field(default=False, sa_column_kwargs={"server_default": false()})
    oidc_org_id: str = Field(sa_type=Text, unique=True)

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

    async def delete(self) -> None:
        """Delete this group; the `group_demote_scopes` trigger resets its scoped rows to private.

        A `uuid[]` scope-set column carries no foreign key, so Postgres cannot cascade a group
        deletion into the rows naming it the way it does for `membership`. The `BEFORE DELETE`
        trigger on `group_` (migration `0005`) runs that demotion in the database, dropping a claim
        whose owner already holds the same content privately and resetting every other scoped row
        to `{}`, so this method only deletes the row and lets the trigger widen its shared rows
        back to private on the way out.
        """
        await session().delete(self)
