import uuid
from typing import ClassVar

from sqlalchemy import Index, Text, UniqueConstraint
from sqlalchemy.orm import declared_attr
from sqlmodel import Field

from ....common.sql import Column
from ...mixins import Embedded, Id, Scoped, TableBase, Timestamped


class Profile(Id, Scoped, Timestamped, Embedded, TableBase, table=True):
    """Scoped, embedded summary of an entity's current facts."""

    mutable: ClassVar[bool] = True

    subject_id: Column[uuid.UUID] = Field(
        foreign_key="entity_content.id", ondelete="CASCADE", nullable=False, index=True
    )
    summary: Column[str] = Field(sa_type=Text)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index | UniqueConstraint, ...]:
        return (
            *super().__table_args__,
            Index("ix_profile_scopes", "scopes", postgresql_using="gin"),
            UniqueConstraint("scopes", "subject_id", name="uq_profile_scope_subject"),
        )
