from datetime import datetime
from typing import ClassVar

from patos import sql
from pydantic import UUID7, UUID8
from sqlalchemy import Column as SAColumn
from sqlalchemy import DateTime, Index, UniqueConstraint, and_, column, or_
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Field, Relationship

from ...mixins import Id, Scoped, TableBase, Timestamped
from .chunk import Chunk


class Document(Id, Scoped, Timestamped, TableBase, table=True):
    """Scoped source item and parent of its ordered chunks."""

    mutable: ClassVar[bool] = True

    __table_args__ = (
        Index("ix_document_scopes", "scopes", postgresql_using="gin"),
        Index(
            "uq_document_subject_title_scope",
            "subject_type",
            "title",
            "scopes",
            unique=True,
            postgresql_where=(column("subject_type").is_not(None) & column("title").is_not(None)),
        ),
        UniqueConstraint("source_uri", "scopes", name="uq_document_source_scope"),
    )

    title: sql.Column[str | None] = Field(default=None)
    subject_type: sql.Column[str | None] = Field(
        default=None,
        foreign_key="entity_kind.name",
    )
    source_uri: sql.Column[str | None] = Field(default=None)
    observed_at: sql.Column[datetime | None] = Field(
        default=None,
        sa_column=SAColumn(DateTime(timezone=True), index=True),
    )
    expires_at: sql.Column[datetime | None] = Field(
        default=None,
        sa_column=SAColumn(DateTime(timezone=True), index=True),
    )
    content_hash: sql.Column[UUID8] = Field(index=True)
    promoted_from: sql.Column[UUID7 | None] = Field(default=None, foreign_key="document.id")

    chunks: list[Chunk] = Relationship(
        cascade_delete=True,
        passive_deletes=True,
        sa_relationship_kwargs={"order_by": "Chunk.ord"},
    )

    @classmethod
    def identifies(
        cls,
        *,
        subject_type: str | None,
        title: str,
        source_uri: str | None,
        content_hash: UUID8,
    ) -> ColumnElement[bool]:
        """Match a source locator or a declared ontology subject."""
        locator = (
            cls.source_uri == source_uri
            if source_uri is not None
            else cls.content_hash == content_hash
        )
        if subject_type is None:
            return locator
        return or_(
            locator,
            and_(cls.subject_type == subject_type, cls.title == title),
        )

    @classmethod
    def identity_key(
        cls,
        *,
        subject_type: str | None,
        title: str | None,
        source_uri: str | None,
        content_hash: UUID8,
    ) -> tuple[str, str, str | UUID8]:
        """Return the batch lookup key corresponding to `identifies`."""
        if subject_type is not None and title is not None:
            return "subject", subject_type, title
        return "source", "", source_uri or content_hash
