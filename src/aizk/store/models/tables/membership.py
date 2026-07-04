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
    """A principal's membership in a group, the edge the RLS predicates walk.

    principal_id: member principal, cascading on delete.
    group_id: group joined, cascading on delete.
    role: standing within the group. Any role grants read visibility, while writing into the
        group's scope requires writer or admin, the read/write split the write policies enforce.

    Carries no `principal` or `group` relationship of its own: every read site already holds the
    id and looks the row up directly rather than navigating from a loaded `Membership`, so the two
    would-be relationships stay unwritten rather than shipped unused.
    """

    class Role(StrEnum):
        """A principal's standing within a group, read-only, writing, or administering it."""

        reader = auto()
        writer = auto()
        admin = auto()

    principal_id: uuid.UUID = Field(
        foreign_key="principal.id", ondelete="CASCADE", primary_key=True
    )
    group_id: uuid.UUID = Field(foreign_key="group_.id", ondelete="CASCADE", primary_key=True)
    role: Role = Field(
        default=Role.writer,
        nullable=False,
        sa_type=cast(type[Role], SAEnum(Role, name="membership_role")),
    )

    @classmethod
    def writable_group_ids(cls, principal_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
        """Selectable of the group ids a principal holds a writer or admin role in.

        The application-side mirror of the write policies' role subquery, for the passes that must
        target only rows they may write.

        principal_id: principal whose writable groups are selected.
        """
        return (
            select(cls.group_id)
            .where(cls.principal_id == principal_id)
            .where(cls.role != cls.Role.reader)
        )

    @classmethod
    def writable_scopes(
        cls, column: InstrumentedAttribute[list[uuid.UUID]], principal_id: uuid.UUID
    ) -> ColumnElement[bool]:
        """Boolean clause selecting rows whose scope set the principal may write, private included.

        A row is writable when it is private (an empty scope set) or its whole scope set is
        contained in the groups this principal writes into, the application-side mirror of
        `store.mixins.scoped.ScopeLattice.write`'s own containment shape, for the passes that must
        target only rows they may write rather than relying on row level security to filter a
        write attempt after the fact.

        column: the scope-set column of the model being filtered.
        principal_id: principal whose write access filters the rows.
        """
        writable = select(
            func.coalesce(func.array_agg(cls.group_id), ScopeLattice.empty_scopes())
        ).where(cls.principal_id == principal_id, cls.role != cls.Role.reader)
        return (func.cardinality(column) == 0) | column.contained_by(writable.scalar_subquery())
