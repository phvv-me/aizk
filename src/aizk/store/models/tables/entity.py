from collections.abc import Sequence
from typing import ClassVar, cast

from patos import sql
from pydantic import UUID5
from sqlalchemy import Index, Table, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import declared_attr
from sqlmodel import select
from sqlmodel.sql.expression import SelectOfScalar

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

    @classmethod
    def roster(cls, scopes: Sequence[UUID5], limit: int) -> SelectOfScalar[str]:
        """The lowered entity names in one exact scope set, the egress sanitizer's roster.

        The join runs through the scoped claim rather than the shared content table, so row
        security decides what the roster can contain at all and the caller's own scope set
        narrows it further. That narrowing matters, because a name in a public organization
        belongs to everyone who can read it rather than to this caller, and the same list is
        checked as literal substrings against every rewritten query.

        The longest names come first, since a long name is the one that identifies somebody
        and a short one is usually a word, so a roster cut by `limit` keeps the entries that
        were worth checking.

        scopes: the exact claim scopes whose names count, normally the caller's writable set.
        limit: how many names the sanitizer will hold, bounding one substring pass.
        """
        # The distinct cut is its own subquery, because PostgreSQL requires every ORDER BY
        # expression of a SELECT DISTINCT to appear in its select list, and ordering by a
        # name's length is exactly an expression that does not.
        names = (
            select(func.lower(cls.name).label("name"))
            .join(EntityClaim, EntityClaim.content_id == cls.id)
            .where(EntityClaim.scopes.overlap(list(scopes)))
            .distinct()
            .subquery("roster_name")
        )
        return cast(
            "SelectOfScalar[str]",
            select(names.c.name)
            .order_by(func.length(names.c.name).desc(), names.c.name)
            .limit(limit),
        )
