import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.orm import declared_attr
from sqlmodel import Field

from ..mixins import Embedded, Id, Scoped, TableBase


class Profile(Id, Scoped, Embedded, TableBase, table=True):
    """A rolled-up portrait of one entity, the static-plus-dynamic memory recall surfaces.

    Where a fact is one edge, a profile is the whole subject seen at once, the latest facts about
    an entity summarized into a short paragraph the recall lane can lift wholesale when that entity
    is the top match. The summary embedding lets profile lookup rank by the entity's name, and the
    row is scoped and row-level-security-forced exactly like the entities and facts it builds from.

    id: stable identity, generated client-side on insert.
    owner_id: principal that owns the row, enforced by row level security.
    scope: group the row is shared with, null when private to the owner.
    subject_id: entity content the profile portrays, cascading on delete.
    summary: short static-plus-dynamic paragraph rolled up from the entity's latest facts.
    embedding: halfvec dense vector of the summary, what profile lookup ranks.
    updated_at: last time the profile was rebuilt.
    """

    subject_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("entity_content.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    summary: str = Field(sa_type=Text)
    updated_at: datetime = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
        ),
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index | UniqueConstraint, ...]:
        # one profile per owner-and-subject pair, the upsert key ProfileTierBuilder.upsert's
        # postgresql insert().on_conflict_do_update targets so a rebuild overwrites the row in
        # place instead of racing a concurrent rebuild into a duplicate.
        return (
            *super().__table_args__,
            UniqueConstraint("owner_id", "subject_id", name="uq_profile_owner_subject"),
        )
