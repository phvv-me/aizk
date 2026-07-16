from datetime import datetime
from typing import ClassVar

from patos import sql
from pydantic import UUID7
from sqlalchemy import Column as SAColumn
from sqlalchemy import DateTime, Index, Text, UniqueConstraint
from sqlalchemy.orm import declared_attr
from sqlmodel import Field

from ...mixins import Embedded, Id, Scoped, TableBase


class Chunk(Id, Scoped, Embedded, TableBase, table=True):
    """Store one ordered source span with parent-inherited visibility and retrieval indexes."""

    mutable: ClassVar[bool] = True
    deletable: ClassVar[bool] = True
    read_through: ClassVar[str] = "document"

    document_id: sql.Column[UUID7] = Field(
        foreign_key="document.id", ondelete="CASCADE", nullable=False, index=True
    )
    ord: sql.Column[int]
    text: sql.Column[str] = Field(sa_type=Text)
    lexical: sql.Column[str | None] = Field(default=None, sa_type=Text)
    tokens: sql.Column[int | None] = Field(default=None)
    provenance: sql.Column[dict] = Field(
        default_factory=dict, sa_type=sql.TypedJSONB, sa_column_kwargs={"server_default": "{}"}
    )
    processed_at: sql.Column[datetime | None] = Field(
        default=None, sa_column=SAColumn(DateTime(timezone=True))
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Index | UniqueConstraint, ...]:
        return (
            *super().__table_args__,
            Index("ix_chunk_scopes", "scopes", postgresql_using="gin"),
            Index(
                "ix_chunk_pending",
                "id",
                postgresql_where=SAColumn("processed_at").is_(None),
            ),
        )
