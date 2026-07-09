import uuid
from enum import StrEnum, auto
from typing import cast

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import InstrumentedAttribute
from sqlmodel import Field

from ...mixins import TableBase
from ...mixins.scoped import ScopeLattice


class Membership(TableBase, table=True):
    """A user's membership in a group, the edge the RLS predicates walk.

    user_id: member user, cascading on delete.
    group_id: group joined, cascading on delete.
    role: standing within the group, named to match Logto's organization roles verbatim so a
        token's role folds straight into this enum with no translation. Any role grants read
        visibility, while writing into the group's scope requires editor or admin, the read/write
        split the write policies enforce.

    Carries no `user` or `group` relationship of its own, since every read site already holds
    the id and looks the row up directly rather than navigating from a loaded `Membership`, so the
    two would-be relationships stay unwritten rather than shipped unused.
    """

    class Role(StrEnum):
        """A user's standing within a group, read-only, writing, or administering it, the same
        `viewer`/`editor`/`admin` names Logto's organization roles carry so no mapping sits
        between the token and this enum.
        """

        viewer = auto()
        editor = auto()
        admin = auto()

    user_id: uuid.UUID = Field(foreign_key="users.id", ondelete="CASCADE", primary_key=True)
    group_id: uuid.UUID = Field(foreign_key="group_.id", ondelete="CASCADE", primary_key=True)
    role: Role = Field(
        default=Role.editor,
        nullable=False,
        sa_type=cast(type[Role], SAEnum(Role, name="membership_role")),
    )

    @classmethod
    def writable_group_ids(cls, user_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
        """Selectable of the group ids a user holds an editor or admin role in.

        The application-side mirror of the write policies' role subquery, for the passes that must
        target only rows they may write.

        user_id: user whose writable groups are selected.
        """
        return (
            select(cls.group_id).where(cls.user_id == user_id).where(cls.role != cls.Role.viewer)
        )

    @classmethod
    def writable_scopes(
        cls,
        scopes: InstrumentedAttribute[list[uuid.UUID]],
        owner: InstrumentedAttribute[uuid.UUID],
        user_id: uuid.UUID,
    ) -> ColumnElement[bool]:
        """Boolean clause selecting rows whose scope set the user may write, their own private
        rows included.

        A row is writable when it is the user's own private row (an empty scope set they own) or a
        shared row whose whole scope set is contained in the groups this user writes into, the
        application-side mirror of `store.mixins.scoped.ScopeLattice.write`'s containment shape,
        for the passes that must target only rows they may write rather than relying on row level
        security to filter a write attempt after the fact. The empty-scope branch is guarded by
        ownership because `'{}' <@ anything` is trivially true, so an unguarded containment check
        would call every private row writable regardless of who owns it.

        scopes: the scope-set column of the model being filtered.
        owner: the owner column of the same model, gating the private branch.
        user_id: user whose write access filters the rows.
        """
        writable = select(
            func.coalesce(func.array_agg(cls.group_id), ScopeLattice.empty_scopes())
        ).where(cls.user_id == user_id, cls.role != cls.Role.viewer)
        return ((func.cardinality(scopes) == 0) & (owner == user_id)) | (
            (func.cardinality(scopes) > 0) & scopes.contained_by(writable.scalar_subquery())
        )
