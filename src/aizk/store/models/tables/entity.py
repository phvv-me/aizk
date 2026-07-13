import uuid
from typing import ClassVar

from sqlalchemy import Index, Table, Text, UniqueConstraint
from sqlalchemy.orm import declared_attr
from sqlmodel import Field

from ....common.sql import Column, TypedJSONB
from ...mixins import ClaimedContent, Embedded, Id, Scoped, TableBase, Timestamped


class EntityClaim(Id, Scoped, Timestamped, TableBase, table=True):
    """One scope set's access and metadata for a canonical entity."""

    content_id: Column[uuid.UUID] = Field(
        foreign_key="entity_content.id",
        ondelete="CASCADE",
        nullable=False,
        index=True,
    )
    attributes: Column[dict] = Field(
        default_factory=dict,
        sa_type=TypedJSONB,
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[UniqueConstraint | Index, ...]:
        return (
            UniqueConstraint(
                "content_id",
                "scopes",
                name="uq_entity_claim_content_scope",
            ),
            Index("ix_entity_claim_scopes", "scopes", postgresql_using="gin"),
        )


class EntityContent(Id, Embedded, ClaimedContent, TableBase, table=True):
    """Canonical entity identity shared through authorized scoped claims."""

    name: Column[str] = Field(sa_type=Text)
    type: Column[str] = Field(sa_type=Text, foreign_key="entity_kind.name")
    claim_table: ClassVar[Table] = EntityClaim.__table__
