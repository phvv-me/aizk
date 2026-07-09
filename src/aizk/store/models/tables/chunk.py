import uuid
from datetime import datetime

from sqlalchemy import Column, Computed, DateTime, Index, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import declared_attr
from sqlmodel import Field

from ...mixins import Embedded, Id, Scoped, TableBase


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
    processed_at: when the graph build last ran extraction and consolidation over this chunk, null
        until the first pass. Set regardless of whether that pass minted any claim, so `chunk`'s
        own column, not an anti-join against `fact_claim`, is what `pending_chunks` reads. A chunk
        whose prose asserts nothing worth keeping still finished a real pass and stays finished.
    owner_id: user that owns the row, enforced by row level security.
    scopes: org set the row is shared with, an implicit intersection when it names more than
        one, empty when private to the owner.

    Carries no `document` relationship of its own, since every read site already holds the
    document id (`Chunk.document_id`, the indexed FK) and filters or joins on it directly rather
    than navigating from a loaded `Chunk`, so a back-reference here would ship unused.
    """

    # indexed: promote's document-ordered rebuild and build_graph's source-title filter both
    # reverse-look-up a document's chunks by this column; EXPLAIN against a seeded corpus showed
    # the unindexed lookup falling back to a full scan of the chunk table
    document_id: uuid.UUID = Field(
        foreign_key="document.id", ondelete="CASCADE", nullable=False, index=True
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
    processed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index, ...]:
        # scopes earns its own GIN index here, unlike most scoped tables, since promotion copies
        # and RLS reads both filter chunks by target scope-set often; declared as a table arg
        # rather than a redeclared mixin Field so pydantic never sees the child shadowing
        # Scoped.scopes. The pending index is partial, only the still-unprocessed rows, mirroring
        # the migration's own `ix_chunk_pending`, so alembic's autogenerate never sees the ORM and
        # the DDL drift apart.
        return (
            *super().__table_args__,
            Index("ix_chunk_tsv", "tsv", postgresql_using="gin"),
            Index("ix_chunk_scopes", "scopes", postgresql_using="gin"),
            Index(
                "ix_chunk_pending",
                "id",
                postgresql_where=Column("processed_at").is_(None),
            ),
        )
