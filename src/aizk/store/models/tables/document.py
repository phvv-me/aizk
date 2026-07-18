from datetime import datetime
from typing import ClassVar, Self

from patos import sql
from patos.sql import Column as C
from pydantic import UUID7, UUID8
from sqlalchemy import Column as SAColumn
from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    and_,
    bindparam,
    column,
    func,
    or_,
)
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Field, Relationship, select
from sqlmodel.sql.expression import SelectOfScalar

from ...mixins import Id, Scoped, TableBase, Timestamped
from .artifact import Artifact
from .chunk import Chunk
from .ontology import EntityKind


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
        ForeignKeyConstraint(
            ("artifact_id", "artifact_content_id"),
            ("artifact_content.artifact_id", "artifact_content.id"),
            name="fk_document_artifact_content_pair",
            ondelete="SET NULL",
        ),
    )

    title = sql.Nullable(str)
    subject_type = sql.FK(EntityKind.name, nullable=True)
    source_uri = sql.Nullable(str)
    observed_at: C[datetime | None] = Field(
        default=None,
        sa_column=SAColumn(DateTime(timezone=True), index=True),
    )
    expires_at: C[datetime | None] = Field(
        default=None,
        sa_column=SAColumn(DateTime(timezone=True), index=True),
    )
    artifact_id = sql.FK(
        Artifact.id,
        nullable=True,
        ondelete="SET NULL",
        index=True,
    )
    # The reference to a content revision is carried by the composite foreign key in
    # `__table_args__` so PostgreSQL guarantees the pair belongs to `artifact_id`.
    artifact_content_id = sql.Field(UUID7 | None, default=None, index=True)
    content_hash: C[UUID8] = Field(index=True)
    promoted_from: C[UUID7 | None] = Field(default=None, foreign_key="document.id")

    chunks: list[Chunk] = Relationship(
        cascade_delete=True,
        passive_deletes=True,
        sa_relationship_kwargs={"order_by": "Chunk.ord"},
    )

    @classmethod
    def newest(cls, limit: int) -> SelectOfScalar[Self]:
        """The most recently updated visible documents, newest first.

        limit: how many documents to keep.
        """
        return (
            select(cls)
            .order_by(cls.__table__.c.updated_at.desc(), cls.__table__.c.id.desc())
            .limit(limit)
        )

    @classmethod
    def is_active(cls) -> ColumnElement[bool]:
        """Whether a source has no expiry or remains valid at database time."""
        return or_(cls.expires_at.is_(None), cls.expires_at > func.now())

    @classmethod
    def named_in_query(cls) -> ColumnElement[bool]:
        """Whether the query contains the source's complete title."""
        query = func.lower(bindparam("qtext", type_=Text))
        return func.strpos(query, cls.title.lower()) > 0

    @classmethod
    def identifies(
        cls,
        *,
        subject_type: str | None,
        title: str,
        source_uri: str | None,
        artifact_id: UUID7 | None,
        content_hash: UUID8,
    ) -> ColumnElement[bool]:
        """Match a source locator or a declared ontology subject."""
        if artifact_id is not None:
            return cls.artifact_id == artifact_id
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
        artifact_id: UUID7 | None,
        content_hash: UUID8,
    ) -> tuple[str, str, str | UUID7 | UUID8]:
        """Return the batch lookup key corresponding to `identifies`."""
        if artifact_id is not None:
            return "artifact", "", artifact_id
        if subject_type is not None and title is not None:
            return "subject", subject_type, title
        return "source", "", source_uri or content_hash
