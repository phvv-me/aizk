import uuid
from typing import ClassVar

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, Relationship

from ....common.sql import Column
from ...mixins import Id, Scoped, TableBase, Timestamped
from .chunk import Chunk


class Document(Id, Scoped, Timestamped, TableBase, table=True):
    """Scoped source item and parent of its ordered chunks."""

    mutable: ClassVar[bool] = True

    __table_args__ = (
        Index("ix_document_scopes", "scopes", postgresql_using="gin"),
        UniqueConstraint("source_uri", "scopes", name="uq_document_source_scope"),
    )

    kind: Column[str] = Field(default="note")
    title: Column[str | None] = Field(default=None)
    source_uri: Column[str | None] = Field(default=None)
    content_hash: Column[str] = Field(index=True)
    promoted_from: Column[uuid.UUID | None] = Field(default=None, foreign_key="document.id")

    chunks: list[Chunk] = Relationship(
        cascade_delete=True,
        passive_deletes=True,
        sa_relationship_kwargs={"order_by": "Chunk.ord"},
    )
