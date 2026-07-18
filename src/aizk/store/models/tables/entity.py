from collections.abc import Sequence
from typing import ClassVar

from patos import sql
from pydantic import UUID5
from sqlalchemy import Index, Table, UniqueConstraint
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import declared_attr

from ....types import Scopes
from ...engine import Session
from ...mixins import ClaimedContent, DeterministicId, Embedded, Id, Scoped, TableBase, Timestamped
from .ontology import EntityKind


class EntityClaim(Id, Scoped, Timestamped, TableBase, table=True):
    """One scope set's access and metadata for a canonical entity."""

    content_id = sql.Field(
        UUID5,
        foreign_key="entity_content.id",
        ondelete="CASCADE",
        index=True,
    )
    attributes = sql.Field(
        dict,
        default_factory=dict,
        sa_type=sql.TypedJSONB,
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

    @classmethod
    async def claim_all(
        cls,
        session: Session,
        content_ids: Sequence[UUID5],
        created_by: UUID5,
        scopes: Scopes,
    ) -> None:
        """Idempotently claim canonical entities together inside one exact scope set."""
        if not content_ids:
            return
        await session.exec(
            insert(cls)
            .values(
                [
                    {
                        "content_id": content_id,
                        "created_by": created_by,
                        "scopes": sorted(scopes),
                    }
                    for content_id in dict.fromkeys(content_ids)
                ]
            )
            .on_conflict_do_nothing(index_elements=[cls.content_id, cls.scopes])
        )


class EntityContent(DeterministicId, Embedded, ClaimedContent, TableBase, table=True):
    """Canonical entity identity shared through authorized scoped claims."""

    name = sql.Field(str)
    type = sql.FK(EntityKind.name)
    claim_table: ClassVar[Table] = EntityClaim.__table__
