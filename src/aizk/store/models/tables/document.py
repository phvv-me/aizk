from collections.abc import Collection
from datetime import datetime
from typing import ClassVar, Self

from patos import sql
from patos.sql import Column as C
from pydantic import UUID5, UUID7, UUID8
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
    update,
)
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import ScalarSelect
from sqlmodel import Field, Relationship, select
from sqlmodel.sql.expression import Select, SelectOfScalar

from ...engine import Session
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
        # One source may stand for at most one copy per destination. The database owns that
        # rule because two concurrent shares would otherwise both find no standing copy and
        # both insert one. It also indexes the `promoted_from` lookup every share performs.
        Index(
            "uq_document_promotion_scope",
            "promoted_from",
            "scopes",
            unique=True,
            postgresql_where=column("promoted_from").is_not(None),
        ),
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
    artifact_content_id = sql.Field(
        UUID7 | None,
        default=None,
        index=True,
    )
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
    def newest_authored(cls, limit: int) -> SelectOfScalar[Self]:
        """The newest visible documents created from text rather than preserved files."""
        return cls.newest(limit).where(cls.artifact_id.is_(None))

    @classmethod
    def authored_total(cls) -> ScalarSelect[int]:
        """Count visible documents created from text rather than preserved files."""
        return (
            select(func.count(cls.__table__.c.id))
            .where(cls.artifact_id.is_(None))
            .scalar_subquery()
        )

    @classmethod
    def is_active(cls) -> ColumnElement[bool]:
        """Whether a source has no expiry or remains valid at database time."""
        return or_(cls.expires_at.is_(None), cls.expires_at > func.now())

    @classmethod
    def shareable(
        cls, document_ids: Collection[UUID7], owner: UUID5 | None = None
    ) -> Select[tuple[UUID7, str | None, list[UUID5]]]:
        """The named documents a share may carry, with the title and scope set it judges by.

        Row security already hides what the caller cannot read, so an unrestricted selection
        is every visible named document. `owner` narrows it to the caller's own private
        documents, the guard a query-driven or moving share needs: a broad question must not
        sweep an organization's documents somewhere else, and a move must never pull evidence
        out from under the other members of a shared scope. An already retired original stays
        selectable, which is what lets a repeated move settle on the copy it already made.
        The scope set travels back so the caller can drop a document that already stands in
        the destination, since promoting one into its own scope would only breed generations.
        """
        statement = select(cls.id, cls.title, cls.scopes).where(cls.id.in_(document_ids))
        if owner is None:
            return statement
        return statement.where(cls.scopes == [owner])

    def active_at(self, moment: datetime) -> bool:
        """Whether this source still holds at `moment`, the Python twin of `is_active`."""
        return self.expires_at is None or self.expires_at > moment

    @classmethod
    async def retire(cls, session: Session, document_ids: Collection[UUID7]) -> list[UUID7]:
        """Expire moved originals now so ordinary recall stops returning them.

        Expiry is the engine's one erasure for a source: every chunk ranking joins through
        `is_active`, so an expired document leaves recall in the same statement that keeps
        its rows, its bytes, and the `promoted_from` chain intact for provenance and for a
        move back. The guard leaves an already-expired original alone, which is what makes
        repeating a move a no-op.
        """
        retired = await session.exec(
            update(cls)
            .where(cls.id.in_(document_ids), cls.is_active())
            .values(expires_at=func.now())
            .returning(cls.id)
            .execution_options(synchronize_session=False)
        )
        return [row[0] for row in retired]

    @classmethod
    def named_in_query(cls) -> ColumnElement[bool]:
        """Whether the query contains the source's complete normalized title."""
        pattern = "[^[:alnum:]]+"
        query = func.btrim(
            func.regexp_replace(
                func.lower(bindparam("qtext", type_=Text)),
                pattern,
                " ",
                "g",
            )
        )
        title = func.btrim(func.regexp_replace(cls.title.lower(), pattern, " ", "g"))
        return and_(
            func.length(title) >= 3,
            func.strpos(
                func.concat(" ", query, " "),
                func.concat(" ", title, " "),
            )
            > 0,
        )

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
