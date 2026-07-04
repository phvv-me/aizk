import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Index
from sqlmodel import Field, Relationship

from ...mixins import Id, Scoped, TableBase, Timestamped

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
    scopes: group set the row is shared with, an implicit intersection when it names more than
        one, empty when private to the owner.
    promoted_from: source document this row was copied from when it was promoted to a wider scope,
        null for an original, the provenance link that keeps a promotion auditable and one-way.
    created_at: first-seen timestamp.
    updated_at: last-write timestamp.
    chunks: ordered text spans embedded for hybrid search.
    """

    # scopes earns its own GIN index here like chunk's, since promotion copies and RLS reads both
    # filter documents by target scope-set often; a table arg rather than a redeclared mixin Field
    # so pydantic never sees the child shadowing Scoped.scopes
    __table_args__ = (Index("ix_document_scopes", "scopes", postgresql_using="gin"),)

    kind: str = Field(default="note")
    title: str | None = Field(default=None)
    source_uri: str | None = Field(default=None, unique=True)
    content_hash: str = Field(index=True)
    promoted_from: uuid.UUID | None = Field(default=None, foreign_key="document.id")

    # no back_populates: Chunk carries no `document` relationship of its own, every read site
    # already id-keyed rather than navigating from a loaded Chunk, while `Document.chunks` itself
    # stays, `graph/promote.py`'s document-copy path actually loads and iterates it.
    # cascade_delete=True/passive_deletes=True replace the hand-rolled "all, delete-orphan" cascade
    # plus its passive_deletes kwarg, both first-class Relationship kwargs; order_by has no such
    # first-class kwarg, so it stays in sa_relationship_kwargs alone.
    chunks: list[Chunk] = Relationship(
        cascade_delete=True,
        passive_deletes=True,
        sa_relationship_kwargs={"order_by": "Chunk.ord"},
    )
