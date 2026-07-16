from typing import ClassVar

from patos import sql
from pydantic import UUID5
from sqlalchemy import Index, Text, UniqueConstraint
from sqlalchemy.orm import declared_attr
from sqlmodel import Field

from ...mixins import Embedded, Id, Scoped, TableBase, Timestamped


class Profile(Id, Scoped, Timestamped, Embedded, TableBase, table=True):
    """Scoped, embedded summary of an entity's current facts."""

    mutable: ClassVar[bool] = True

    subject_id: sql.Column[UUID5] = Field(
        foreign_key="entity_content.id", ondelete="CASCADE", nullable=False, index=True
    )
    summary: sql.Column[str] = Field(sa_type=Text)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index | UniqueConstraint, ...]:
        return (
            *super().__table_args__,
            Index("ix_profile_scopes", "scopes", postgresql_using="gin"),
            UniqueConstraint("scopes", "subject_id", name="uq_profile_scope_subject"),
        )
