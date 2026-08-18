from collections.abc import Collection
from datetime import datetime
from enum import auto
from typing import TYPE_CHECKING, ClassVar, Self

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
from sqlmodel import Field, Relationship, select
from sqlmodel.sql.expression import Select, SelectOfScalar

from ...engine import Session
from ...mixins import Id, Scoped, TableBase, Timestamped
from .artifact import Artifact
from .ontology import EntityKind

if TYPE_CHECKING:
    from .chunk import Chunk


class Document(Id, Scoped, Timestamped, TableBase, table=True):
    """Scoped source item and parent of its ordered chunks."""

    class Origin(sql.PGEnum):
        """Where a document's text came from.

        `web_cache` marks a page `find` fetched from a third party and kept so the next
        question does not pay for it again. It is quarantined knowledge. It is never
        projected into the graph, never read by the ontology or insight passes, and always
        renders under the web provenance label even when ordinary retrieval surfaces it, so
        a stranger's page can never be mistaken for something the caller wrote.
        """

        authored = auto()
        web_cache = auto()

    mutable: ClassVar[bool] = True
    # A fetched page's locator lives in its own URI namespace. `uq_document_source_scope`
    # makes `(source_uri, scopes)` unique, so an unnamespaced cache row would compete for
    # the very slot an authored note holding the same URL already occupies.
    cache_scheme: ClassVar[str] = "web-cache:"

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
    origin = sql.Field(Origin, default=Origin.authored, index=True)

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
    def is_active(cls) -> ColumnElement[bool]:
        """Whether a source has no expiry or remains valid at database time."""
        return or_(cls.expires_at.is_(None), cls.expires_at > func.now())

    @classmethod
    def cache_locator(cls, url: str) -> str:
        """Namespace a fetched page's address so it cannot collide with an authored source."""
        return f"{cls.cache_scheme}{url}"

    @classmethod
    def public_url(cls, source_uri: str | None) -> str | None:
        """The address a source really came from, with any cache namespace removed."""
        if source_uri is None:
            return None
        return source_uri.removeprefix(cls.cache_scheme)

    @classmethod
    def projectable(cls) -> ColumnElement[bool]:
        """Whether a source's chunks may enter the knowledge graph at all.

        A cached web page is a stranger's text kept only so the next question does not pay
        to fetch it again. Extracting entities and facts out of it would put third-party
        claims into the caller's own graph and from there into profiles, communities and
        insights, so the recovery sweep skips it exactly as the fetch path never enqueued it.
        """
        return cls.origin != cls.Origin.web_cache

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
        """Expire moved originals now so ordinary find stops returning them.

        Expiry is the engine's one erasure for a source: every chunk ranking joins through
        `is_active`, so an expired document leaves find in the same statement that keeps
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
        origin: Origin = Origin.authored,
    ) -> ColumnElement[bool]:
        """Match a source locator or a declared ontology subject, within one origin.

        Origin is part of identity rather than a field a match carries along. Ingestion
        resolves a title and an ontology subject out of the text itself, and the text of a
        fetched page is a stranger's, so without this a page whose Markdown opened like an
        existing note would resolve to that note and refresh it. Keeping the two origins in
        separate identity spaces means the worst such a page can do is revise an earlier
        copy of itself.
        """
        same_origin = cls.origin == origin
        if artifact_id is not None:
            return and_(same_origin, cls.artifact_id == artifact_id)
        locator = (
            cls.source_uri == source_uri
            if source_uri is not None
            else cls.content_hash == content_hash
        )
        if subject_type is None:
            return and_(same_origin, locator)
        return and_(
            same_origin,
            or_(locator, and_(cls.subject_type == subject_type, cls.title == title)),
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
        origin: Origin = Origin.authored,
    ) -> tuple[str, str, str, str | UUID7 | UUID8]:
        """Return the batch lookup key corresponding to `identifies`."""
        if artifact_id is not None:
            return origin.value, "artifact", "", artifact_id
        if subject_type is not None and title is not None:
            return origin.value, "subject", subject_type, title
        return origin.value, "source", "", source_uri or content_hash
