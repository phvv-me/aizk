import uuid
from datetime import datetime
from typing import ClassVar

from sqlalchemy import Column as SAColumn
from sqlalchemy import DateTime, Index, Text, UniqueConstraint
from sqlalchemy.orm import declared_attr
from sqlmodel import Field

from ....common.sql import Column, TypedJSONB
from ...mixins import Embedded, Id, Scoped, TableBase


class Chunk(Id, Scoped, Embedded, TableBase, table=True):
    """Ordered document span with dense, lexical, and provenance data."""

    mutable: ClassVar[bool] = True
    deletable: ClassVar[bool] = True
    read_through: ClassVar[str] = "document"

    document_id: Column[uuid.UUID] = Field(
        foreign_key="document.id", ondelete="CASCADE", nullable=False, index=True
    )
    ord: Column[int]
    text: Column[str] = Field(sa_type=Text)
    lexical: Column[str | None] = Field(default=None, sa_type=Text)
    tokens: Column[int | None] = Field(default=None)
    provenance: Column[dict] = Field(
        default_factory=dict, sa_type=TypedJSONB, sa_column_kwargs={"server_default": "{}"}
    )
    processed_at: Column[datetime | None] = Field(
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
