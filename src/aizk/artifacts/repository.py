from datetime import UTC, datetime

from pydantic import UUID7
from sqlalchemy import or_
from sqlmodel import select, update

from ..storage import IntegrityCheck, StoredBytes, StoredObject
from ..store import Artifact, Blob
from ..store.identity import User
from ..store.models.tables import ArtifactContent
from ..types import Scopes
from .models import ArtifactReceipt, ConvertedArtifact, OriginalArtifact, OriginalDescription


def _stored_object(row: Blob) -> StoredObject:
    """Project one blob row onto the object reference a storage pass consumes."""
    return StoredObject(
        id=row.id,
        key=row.storage_key,
        content_hash=row.content_hash,
        size=row.size,
        stored_size=row.stored_size,
        encoding=row.encoding,
        version=row.storage_version,
    )


class ArtifactRepository:
    """Persist artifact metadata through exact caller-bound PostgreSQL transactions."""

    async def create_original(
        self,
        user: User,
        stored: StoredBytes,
        described: OriginalDescription,
        scopes: Scopes,
    ) -> ArtifactReceipt:
        """Create a logical artifact revision that references one immutable stored object."""
        ordered_scopes = sorted(scopes, key=str)
        async with user as session:
            artifact = None
            if described.source_uri is not None:
                artifact = (
                    await session.exec(
                        select(Artifact).where(
                            Artifact.source_uri == described.source_uri,
                            Artifact.scopes == ordered_scopes,
                        )
                    )
                ).first()
            if artifact is None:
                artifact = Artifact(
                    name=described.filename,
                    source_uri=described.source_uri,
                    created_by=user.id,
                    scopes=ordered_scopes,
                )
                session.add(artifact)
                await session.flush()
            else:
                artifact.name = described.filename
            revision = (
                await session.exec(
                    select(Artifact.Content.revision.max(default=0)).where(
                        Artifact.Content.artifact_id == artifact.id
                    )
                )
            ).one() + 1
            blob = Blob(
                **stored.model_dump(by_alias=True),
                media_type=described.media_type,
            )
            session.add(blob)
            await session.flush()
            content = Artifact.Content(
                **described.model_dump(
                    exclude={"filename", "media_type", "source_uri"},
                ),
                artifact_id=artifact.id,
                blob_id=blob.id,
                revision=revision,
                created_by=user.id,
                scopes=ordered_scopes,
            )
            session.add(content)
            await session.flush()
            return ArtifactReceipt(
                artifact_id=artifact.id,
                content_id=content.id,
                state=content.state,
            )

    async def pending(self, user: User, scopes: Scopes, limit: int) -> tuple[UUID7, ...]:
        """Return bounded pending originals in one exact scope for autonomous dispatch."""
        async with user as session:
            rows = await session.exec(
                select(Artifact.Content.id)
                .where(
                    Artifact.Content.state == Artifact.Content.State.pending,
                    Artifact.Content.scopes == sorted(scopes, key=str),
                )
                .order_by(Artifact.Content.created_at, Artifact.Content.id)
                .limit(limit)
            )
            return tuple(rows)

    async def integrity_candidates(
        self,
        stale_before: datetime,
        limit: int,
    ) -> tuple[StoredObject, ...]:
        """Load failed, unverified, or stale object references for one bounded system pass."""
        async with User.system().owner as session:
            rows = (
                await session.exec(
                    select(Blob)
                    .where(
                        or_(
                            Blob.integrity_error.is_not(None),
                            Blob.integrity_checked_at.is_(None),
                            Blob.integrity_checked_at < stale_before,
                        )
                    )
                    .order_by(
                        Blob.integrity_error.is_not(None).desc(),
                        Blob.integrity_checked_at.asc().nulls_first(),
                        Blob.created_at,
                        Blob.id,
                    )
                    .limit(limit)
                )
            ).all()
        return tuple(_stored_object(row) for row in rows)

    async def compaction_candidates(self, level: int, limit: int) -> tuple[StoredObject, ...]:
        """Load the largest objects still laid out under a weaker compression policy.

        Ordering by size puts the reclaimable bytes first, so an operator who runs a few
        bounded batches recovers most of the space before touching the long tail.
        """
        async with User.system().owner as session:
            rows = (
                await session.exec(
                    select(Blob)
                    .where(
                        or_(
                            Blob.encoding_level.is_(None),
                            Blob.encoding_level < level,
                        )
                    )
                    .order_by(Blob.size.desc(), Blob.id)
                    .limit(limit)
                )
            ).all()
        return tuple(_stored_object(row) for row in rows)

    async def record_compaction(
        self,
        blob_id: UUID7,
        level: int,
        verified_at: datetime,
        replacement: StoredBytes | None = None,
    ) -> None:
        """Stamp one evaluated object, moving it when the pass found a denser layout.

        Reaching this point required restoring the object and matching it against the
        stored content hash, so the same transaction records that verification.
        `content_hash` and `size` describe the original bytes and never move.
        """
        async with User.system().owner as session:
            blob = await session.get(Blob, blob_id)
            if blob is None:
                raise LookupError("a compaction candidate disappeared before recording")
            blob.encoding_level = level
            blob.integrity_checked_at = verified_at
            blob.integrity_error = None
            if replacement is not None:
                blob.storage_key = replacement.key
                blob.stored_size = replacement.stored_size
                blob.encoding = replacement.encoding
                blob.etag = replacement.etag
                blob.storage_version = replacement.version

    async def record_integrity(
        self,
        checks: tuple[IntegrityCheck, ...],
        checked_at: datetime,
    ) -> None:
        """Record one pass in one owner transaction without changing immutable metadata."""
        if not checks:
            return
        errors = {check.id: check.error for check in checks}
        async with User.system().owner as session:
            rows = (await session.exec(select(Blob).where(Blob.id.in_(errors)))).all()
            if len(rows) != len(checks):
                raise LookupError("an integrity candidate disappeared before recording")
            for row in rows:
                row.integrity_checked_at = checked_at
                row.integrity_error = errors[row.id]

    async def original(
        self,
        user: User,
        content_id: UUID7,
        scopes: Scopes,
    ) -> OriginalArtifact:
        """Load one visible original and reject a stale or forged queue scope set."""
        async with user as session:
            content = await session.get(Artifact.Content, content_id)
            if content is None:
                raise LookupError("artifact original is not visible")
            if frozenset(content.scopes) != scopes:
                raise PermissionError("artifact queue scopes do not match the stored original")
            artifact = (
                await session.exec(select(Artifact).where(Artifact.id == content.artifact_id))
            ).one()
            blob = (await session.exec(select(Blob).where(Blob.id == content.blob_id))).one()
            return OriginalArtifact(
                artifact_id=artifact.id,
                content_id=content.id,
                revision=content.revision,
                created_by=content.created_by,
                scopes=scopes,
                filename=artifact.name,
                media_type=blob.media_type or "application/octet-stream",
                size=blob.size,
                source_uri=artifact.source_uri,
                companion_text=content.companion_text,
                observed_at=content.observed_at,
                expires_at=content.expires_at,
                storage_key=blob.storage_key,
                storage_version=blob.storage_version,
                storage_hash=blob.content_hash,
                storage_encoding=blob.encoding,
            )

    async def converted(
        self,
        user: User,
        content_id: UUID7,
        scopes: Scopes,
    ) -> ConvertedArtifact:
        """Load one visible original's stored Markdown and the identity it is indexed under.

        Re-chunking needs the same source text the conversion built, and every ingredient of
        that text already sits in PostgreSQL, so nothing here reaches the object store or
        Docling.
        """
        async with user as session:
            content = await session.get(Artifact.Content, content_id)
            if content is None or frozenset(content.scopes) != scopes:
                raise LookupError("artifact original is not visible in its indexing scopes")
            if content.markdown is None:
                raise LookupError("artifact original carries no stored Markdown to re-chunk")
            artifact = (
                await session.exec(select(Artifact).where(Artifact.id == content.artifact_id))
            ).one()
            blob = (await session.exec(select(Blob).where(Blob.id == content.blob_id))).one()
            return ConvertedArtifact(
                artifact_id=artifact.id,
                content_id=content.id,
                created_by=content.created_by,
                scopes=scopes,
                filename=artifact.name,
                media_type=blob.media_type or "application/octet-stream",
                size=blob.size,
                source_uri=artifact.source_uri,
                companion_text=content.companion_text,
                markdown=content.markdown,
                observed_at=content.observed_at,
                expires_at=content.expires_at,
                storage_hash=blob.content_hash,
            )

    async def record_indexing(self, user: User, content_id: UUID7, indexed_at: datetime) -> None:
        """Stamp when one revision's stored Markdown was last split, embedded and indexed.

        The re-chunk sweep orders on this column, so stamping it is what moves the window
        forward and keeps a repeated pass walking the whole corpus instead of the same head.
        """
        async with user as session:
            written = await session.exec(
                update(Artifact.Content)
                .where(Artifact.Content.id == content_id)
                .values(indexed_at=indexed_at)
                .execution_options(synchronize_session=False)
            )
            if not written.rowcount:
                raise LookupError("artifact original disappeared before its indexing was recorded")

    async def set_state(
        self,
        user: User,
        content_id: UUID7,
        scopes: Scopes,
        state: ArtifactContent.State,
        error: str | None = None,
    ) -> None:
        """Advance one visible original while preserving its exact queued scope set.

        The authorization read takes the scope array alone and the change is one `UPDATE`,
        because a state transition three times per conversion has no reason to load and
        decompress the Markdown sitting in the same row.
        """
        async with user as session:
            visible = (
                await session.exec(
                    select(Artifact.Content.scopes).where(Artifact.Content.id == content_id)
                )
            ).first()
            if visible is None or frozenset(visible) != scopes:
                raise LookupError("artifact original is not visible in its queued scopes")
            await session.exec(
                update(Artifact.Content)
                .where(Artifact.Content.id == content_id)
                .values(
                    state=state,
                    error=error,
                    processed_at=datetime.now(UTC)
                    if state
                    in (
                        Artifact.Content.State.ready,
                        Artifact.Content.State.failed,
                        Artifact.Content.State.unreadable,
                    )
                    else None,
                )
                .execution_options(synchronize_session=False)
            )

    async def store_conversion(
        self,
        user: User,
        original: OriginalArtifact,
        markdown: str,
        indexed_at: datetime,
    ) -> None:
        """Store the replaceable Markdown derivative on its exact revision.

        A reconversion overwrites text that is already there, so the authorization read takes
        the scope array rather than the row, which keeps the outgoing Markdown in its TOAST
        pages instead of reading it back to throw it away.
        """
        async with user as session:
            visible = (
                await session.exec(
                    select(Artifact.Content.scopes).where(
                        Artifact.Content.id == original.content_id
                    )
                )
            ).first()
            if visible is None or frozenset(visible) != original.scopes:
                raise LookupError("artifact original is not visible in its conversion scopes")
            await session.exec(
                update(Artifact.Content)
                .where(Artifact.Content.id == original.content_id)
                .values(markdown=markdown, indexed_at=indexed_at)
                .execution_options(synchronize_session=False)
            )
