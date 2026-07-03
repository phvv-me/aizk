import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Column, Computed, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import declared_attr
from sqlmodel import Field, Relationship

from ..mixins import Embedded, Id, Scoped, TableBase

if TYPE_CHECKING:
    from .document import Document


class Chunk(Id, Scoped, Embedded, TableBase, table=True):
    """An ordered text span of a document with its dense embedding and lexical vector.

    id: stable identity, generated client-side on insert.
    document_id: parent document, cascading on delete.
    ord: position of this span within the document.
    text: the span content, the raw span the dense embedding and the rendered recall both read.
    lexical: the text the lexical lanes index, null to fall back to `text`. Contextual ingest fills
        it with a situating preamble prepended to the span, so the bm25 and tsvector lanes match on
        the document's context without the preamble ever polluting the dense vector or the display.
    tokens: token count when measured during ingestion.
    tsv: lexical search vector Postgres derives from `lexical` when set and `text` otherwise, the
        database-maintained generated column the full-text lane matches against, never written by
        the application.
    embedding: halfvec dense vector, null until embedded.
    owner_id: principal that owns the row, enforced by row level security.
    scope: group the row is shared with, null when private to the owner.
    document: parent document relationship.
    """

    # indexed: promote's document-ordered rebuild and build_graph's source-title filter both
    # reverse-look-up a document's chunks by this column; EXPLAIN against a seeded corpus showed
    # the unindexed lookup falling back to a full scan of the chunk table
    document_id: uuid.UUID = Field(
        sa_column=Column(ForeignKey("document.id", ondelete="CASCADE"), nullable=False, index=True)
    )
    ord: int
    text: str = Field(sa_type=Text)
    lexical: str | None = Field(default=None, sa_type=Text)
    tokens: int | None = Field(default=None)
    tsv: str = Field(
        sa_column=Column(
            TSVECTOR,
            Computed("to_tsvector('english', coalesce(lexical, text))", persisted=True),
        )
    )

    document: Document = Relationship(back_populates="chunks")

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index, ...]:
        # scope earns its own index here, unlike most scoped tables, since promotion copies and
        # RLS reads both filter chunks by target scope often; declared as a table arg rather than
        # a redeclared mixin Field so pydantic never sees the child shadowing Scoped.scope
        return (
            *super().__table_args__,
            Index("ix_chunk_tsv", "tsv", postgresql_using="gin"),
            Index("ix_chunk_scope", "scope"),
        )
