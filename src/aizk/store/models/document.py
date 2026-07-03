import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Column, DateTime, ForeignKey, Index, func
from sqlmodel import Field, Relationship

from ..mixins import Id, Scoped, TableBase, Timestamped

if TYPE_CHECKING:
    from .chunk import Chunk


class Document(Id, Scoped, Timestamped, TableBase, table=True):
    """A source item ingested into memory, parent of its ordered chunks.

    id: stable identity, generated client-side on insert.
    kind: coarse type tag such as note, file, or message.
    title: human-readable label when one is known.
    source_uri: unique origin locator used to dedupe re-ingestion.
    content_hash: digest of the source content for change detection.
    owner_id: principal that owns the row, enforced by row level security.
    scope: group the row is shared with, null when private to the owner.
    promoted_from: source document this row was copied from when it was promoted to a wider scope,
        null for an original, the provenance link that keeps a promotion auditable and one-way.
    created_at: first-seen timestamp.
    updated_at: last-write timestamp.
    chunks: ordered text spans embedded for hybrid search.
    """

    # scope earns its own index here like chunk's, since promotion copies and RLS reads both
    # filter documents by target scope often; a table arg rather than a redeclared mixin Field so
    # pydantic never sees the child shadowing Scoped.scope
    __table_args__ = (Index("ix_document_scope", "scope"),)

    kind: str = Field(default="note")
    title: str | None = Field(default=None)
    source_uri: str | None = Field(default=None, unique=True)
    content_hash: str = Field(index=True)
    promoted_from: uuid.UUID | None = Field(
        default=None, sa_column=Column(ForeignKey("document.id"))
    )
    updated_at: datetime = Field(
        default=None,
        sa_column=Column(
            DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
        ),
    )

    chunks: list[Chunk] = Relationship(
        back_populates="document",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "passive_deletes": True,
            "order_by": "Chunk.ord",
        },
    )
