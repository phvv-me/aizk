from datetime import datetime
from enum import auto
from typing import TYPE_CHECKING, ClassVar, Self, cast

from patos import sql
from pydantic import UUID5, UUID7, UUID8, JsonValue
from sqlalchemy import CheckConstraint, Index, UniqueConstraint, false, func, literal, or_
from sqlmodel import Relationship, select
from sqlmodel.sql.expression import Select

from ....exceptions import NotVisibleError
from ...engine import Session
from ...mixins import Id, Scoped, TableBase, Timestamped
from .blob import Blob

if TYPE_CHECKING:
    from .document import Document


class ArtifactContent(Id, Scoped, Timestamped, TableBase, table=True):
    """One immutable original revision and its replaceable PostgreSQL derivatives.

    `state` records the durable business outcome around PgQueuer delivery. PgQueuer
    remains the source of truth for queue leases and retries. The `Blob` contains only
    the losslessly encoded original. Companion text, normalized Markdown, Docling JSON,
    and conversion diagnostics stay queryable in PostgreSQL.
    """

    class State(sql.PGEnum):
        """Durable processing state independent from PgQueuer's delivery state."""

        pending = auto()
        queued = auto()
        processing = auto()
        ready = auto()
        failed = auto()

    mutable: ClassVar[bool] = True
    read_through: ClassVar[str | None] = "artifact"

    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_artifact_content_revision_positive"),
        Index("ix_artifact_content_scopes", "scopes", postgresql_using="gin"),
        UniqueConstraint(
            "artifact_id",
            "revision",
            name="uq_artifact_content_revision",
        ),
        UniqueConstraint(
            "artifact_id",
            "blob_id",
            name="uq_artifact_content_blob",
        ),
        UniqueConstraint(
            "artifact_id",
            "id",
            name="uq_artifact_content_artifact_id_id",
        ),
    )

    artifact_id = sql.Field(
        UUID7,
        foreign_key="artifact.id",
        ondelete="CASCADE",
        index=True,
    )
    blob_id = sql.FK(Blob.id, ondelete="RESTRICT", index=True)
    revision = sql.Field(sql.PositiveInt, default=1)
    state = sql.Field(
        State,
        default=State.pending,
        index=True,
    )
    companion_text = sql.Nullable(str)
    markdown = sql.Nullable(str)
    docling_json = sql.Field(
        dict[str, JsonValue] | None,
        default=None,
        sa_type=sql.TypedJSONB,
    )
    details = sql.Field(
        dict[str, JsonValue],
        default_factory=dict,
        sa_type=sql.TypedJSONB,
    )
    observed_at = sql.Nullable(datetime)
    expires_at = sql.Nullable(datetime)
    error = sql.Nullable(str)
    processed_at = sql.Nullable(datetime)
    blob: Blob = Relationship()

    @classmethod
    def processing_counts(
        cls, one_hour_ago: datetime, six_hours_ago: datetime, day_ago: datetime
    ) -> Select[tuple[int, int, int, int, int, int, datetime | None]]:
        """Caller-visible conversion backlog and recent completions in one row."""
        active = (cls.State.pending, cls.State.queued, cls.State.processing)
        return cast(
            "Select[tuple[int, int, int, int, int, int, datetime | None]]",
            select(
                cls.id.count()
                .filter(cls.state.in_((cls.State.pending, cls.State.queued)))
                .label("queued"),
                cls.id.count().filter(cls.state == cls.State.processing).label("running"),
                cls.id.count().filter(cls.state == cls.State.failed).label("failed"),
                cls.id.count()
                .filter(cls.state == cls.State.ready, cls.processed_at >= one_hour_ago)
                .label("completed_1h"),
            ).add_columns(
                cls.id.count()
                .filter(cls.state == cls.State.ready, cls.processed_at >= six_hours_ago)
                .label("completed_6h"),
                cls.id.count()
                .filter(cls.state == cls.State.ready, cls.processed_at >= day_ago)
                .label("completed_24h"),
                cls.created_at.min().filter(cls.state.in_(active)).label("oldest_at"),
            ),
        )

    @classmethod
    def original(
        cls, artifact_id: UUID7, artifact_content_id: UUID7
    ) -> Select[tuple[str, UUID8, int, Blob.Encoding, list[UUID5], str | None, str | None]]:
        """The authorized object-store fields locating one exact original revision.

        `add_columns` widens the sqlmodel `Select` back to a plain SQLAlchemy `Select`
        statically while the runtime object stays a sqlmodel `Select`, so the cast keeps
        the type `AsyncSession.exec` needs without altering the query.
        """
        return cast(
            "Select[tuple[str, UUID8, int, Blob.Encoding, list[UUID5], str | None, str | None]]",
            select(
                Blob.storage_key,
                Blob.content_hash,
                Blob.size,
                Blob.encoding,
            )
            .add_columns(cls.scopes, Blob.media_type, Blob.storage_version)
            .join(cls, cls.blob_id == Blob.id)
            .where(
                cls.artifact_id == artifact_id,
                cls.id == artifact_content_id,
            )
            .limit(1),
        )


class Artifact(Id, Scoped, Timestamped, TableBase, table=True):
    """Scoped logical file whose immutable bytes live in versioned content rows."""

    Content: ClassVar[type[ArtifactContent]] = ArtifactContent
    mutable: ClassVar[bool] = True

    __table_args__ = (
        CheckConstraint("name <> ''", name="ck_artifact_name_nonempty"),
        Index("ix_artifact_scopes", "scopes", postgresql_using="gin"),
        UniqueConstraint("source_uri", "scopes", name="uq_artifact_source_scope"),
        UniqueConstraint("promoted_from", "scopes", name="uq_artifact_promotion_scope"),
    )

    name = sql.Field(
        sql.NonEmptyString,
        max_length=512,
    )
    description = sql.Nullable(str)
    source_uri = sql.Nullable(str)
    promoted_from = sql.Field(
        UUID7 | None,
        default=None,
        foreign_key="artifact.id",
        index=True,
    )
    contents: list[ArtifactContent] = Relationship(
        cascade_delete=True,
        passive_deletes=True,
        sa_relationship_kwargs={"order_by": ArtifactContent.revision},
    )

    @classmethod
    def recent(cls, limit: int) -> Select[tuple[Self, ArtifactContent]]:
        """Visible originals joined to their revisions, newest accepted first.

        limit: how many revisions to keep.
        """
        return (
            select(cls, ArtifactContent)
            .join(
                ArtifactContent,
                ArtifactContent.artifact_id == cls.id,
            )
            .order_by(
                ArtifactContent.__table__.c.created_at.desc(),
                ArtifactContent.__table__.c.id.desc(),
            )
            .limit(limit)
        )

    @classmethod
    async def share(
        cls,
        session: Session,
        source: Document,
        user_id: UUID5,
        target: list[UUID5],
    ) -> tuple[UUID7 | None, UUID7 | None]:
        """Create target-scoped artifact metadata while reusing the immutable physical
        Blob.

        The content is fetched joined to its parent artifact so a forged pair whose
        `artifact_content_id` belongs to a different artifact cannot carry foreign
        provenance into the target. A transaction-scoped advisory lock keyed by the
        target's dedup identity serializes concurrent shares, so the target-artifact
        lookup, the blob dedup, and `max(revision) + 1` never race a peer.
        """
        if source.artifact_id is None or source.artifact_content_id is None:
            return None, None
        pair = (
            await session.exec(
                select(cls, ArtifactContent)
                .join(ArtifactContent, ArtifactContent.artifact_id == cls.id)
                .where(
                    cls.id == source.artifact_id, ArtifactContent.id == source.artifact_content_id
                )
            )
        ).first()
        if pair is None:
            raise NotVisibleError("the document's original artifact is not visible")
        artifact, content = pair
        target = sorted(set(target), key=str)
        dedup_key = artifact.source_uri if artifact.source_uri is not None else str(artifact.id)
        await session.exec(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(
                        literal(f"artifact_share|{dedup_key}|{','.join(str(s) for s in target)}"),
                        0,
                    )
                )
            )
        )
        target_artifact = (
            await session.exec(
                select(cls).where(
                    or_(
                        cls.promoted_from == artifact.id,
                        cls.source_uri == artifact.source_uri
                        if artifact.source_uri is not None
                        else false(),
                    ),
                    cls.scopes == target,
                )
            )
        ).first()
        if target_artifact is None:
            target_artifact = cls(
                **artifact.model_dump(
                    exclude={
                        "id",
                        "created_at",
                        "updated_at",
                        "created_by",
                        "scopes",
                        "promoted_from",
                    }
                ),
                promoted_from=artifact.id,
                created_by=user_id,
                scopes=target,
            )
            session.add(target_artifact)
            await session.flush()
        standing = (
            await session.exec(
                select(ArtifactContent).where(
                    ArtifactContent.artifact_id == target_artifact.id,
                    ArtifactContent.blob_id == content.blob_id,
                )
            )
        ).first()
        if standing is not None:
            return target_artifact.id, standing.id
        revision = (
            await session.exec(
                select(ArtifactContent.revision.max(default=0)).where(
                    ArtifactContent.artifact_id == target_artifact.id
                )
            )
        ).one() + 1
        shared = ArtifactContent(
            **content.model_dump(
                exclude={
                    "id",
                    "artifact_id",
                    "revision",
                    "created_at",
                    "updated_at",
                    "created_by",
                    "scopes",
                }
            ),
            artifact_id=target_artifact.id,
            revision=revision,
            created_by=user_id,
            scopes=target,
        )
        session.add(shared)
        await session.flush()
        return target_artifact.id, shared.id
