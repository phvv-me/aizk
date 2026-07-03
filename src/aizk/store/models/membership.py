import uuid
from enum import StrEnum, auto
from typing import TYPE_CHECKING

from sqlalchemy import Column, ColumnElement, ForeignKey, Select, select
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import InstrumentedAttribute
from sqlmodel import Field, Relationship

from ..mixins import TableBase

if TYPE_CHECKING:
    from .group import Group
    from .principal import Principal


class Membership(TableBase, table=True):
    """A principal's membership in a group, the edge the RLS predicates walk.

    principal_id: member principal, cascading on delete.
    group_id: group joined, cascading on delete.
    role: standing within the group. Any role grants read visibility, while writing into the
        group's scope requires writer or admin, the read/write split the write policies enforce.
    """

    class Role(StrEnum):
        """A principal's standing within a group, read-only, writing, or administering it."""

        reader = auto()
        writer = auto()
        admin = auto()

    principal_id: uuid.UUID = Field(
        sa_column=Column(ForeignKey("principal.id", ondelete="CASCADE"), primary_key=True)
    )
    group_id: uuid.UUID = Field(
        sa_column=Column(ForeignKey("group_.id", ondelete="CASCADE"), primary_key=True)
    )
    role: Role = Field(
        default=Role.writer, sa_column=Column(SAEnum(Role, name="membership_role"), nullable=False)
    )

    principal: Principal = Relationship(back_populates="memberships")
    group: Group = Relationship()

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
    def writable_scope(
        cls, column: InstrumentedAttribute[uuid.UUID | None], principal_id: uuid.UUID
    ) -> ColumnElement[bool]:
        """Boolean clause selecting rows whose scope the principal may write, private included.

        column: the scope column of the model being filtered.
        principal_id: principal whose write access filters the rows.
        """
        return column.is_(None) | column.in_(cls.writable_group_ids(principal_id))
