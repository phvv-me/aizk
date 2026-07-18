from typing import ClassVar

from patos import sql
from patos.sql import Column as C
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.orm import declared_attr

from ...mixins import Embedded, Id, Scoped, TableBase, Timestamped
from .entity import EntityContent


class Profile(Id, Scoped, Timestamped, Embedded, TableBase, table=True):
    """Scoped, embedded summary of an entity's current facts."""

    mutable: ClassVar[bool] = True

    subject_id = sql.FK(
        EntityContent.id,
        ondelete="CASCADE",
        index=True,
    )
    summary: C[str]

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index | UniqueConstraint, ...]:
        return (
            *super().__table_args__,
            Index("ix_profile_scopes", "scopes", postgresql_using="gin"),
            UniqueConstraint("scopes", "subject_id", name="uq_profile_scope_subject"),
        )
