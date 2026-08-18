from datetime import UTC, datetime

from pydantic import UUID7, JsonValue
from sqlalchemy import func, or_
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import delete, select, update

from ..storage import IntegrityCheck, RetiredObject, StoredBytes, StoredObject
from ..store import Artifact, Blob, ObjectRetirement
from ..store.identity import User
from ..store.locking import acquire_locks
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
        encoding_level=row.encoding_level,
        version=row.storage_version,
    )


def _observed_layout(stored: StoredObject) -> tuple[ColumnElement[bool], ...]:
    """Match exactly the immutable layout one worker actually read."""
    expected_version = (
        Blob.storage_version.is_(None)
        if stored.version is None
        else Blob.storage_version == stored.version
    )
    expected_level = (
        Blob.encoding_level.is_(None)
        if stored.encoding_level is None
        else Blob.encoding_level == stored.encoding_level
    )
    return (
        Blob.id == stored.id,
        Blob.content_hash == stored.content_hash,
        Blob.size == stored.size,
        Blob.storage_key == stored.key,
        expected_version,
        Blob.stored_size == stored.stored_size,
        Blob.encoding == stored.encoding,
        expected_level,
    )


class StorageQuotaExceeded(ValueError):
    """The caller has no room for another original in its stored-byte allowance."""


class ArtifactRepository:
    """Persist artifact metadata through exact caller-bound PostgreSQL transactions."""

    def __init__(self, user_byte_limit: int | None = None) -> None:
        self.user_byte_limit = user_byte_limit

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
            if self.user_byte_limit is not None:
                await acquire_locks(session, [f"artifact-storage|{user.id}"])
                total = (
                    await session.exec(
                        select(func.sum(Blob.size))
                        .select_from(Artifact.Content)
                        .join(Blob, Artifact.Content.blob_id == Blob.id)
                        .where(Artifact.Content.created_by == user.id)
                    )
                ).one()
                used = int(total or 0)
                if used + stored.size > self.user_byte_limit:
                    raise StorageQuotaExceeded(
                        "upload would exceed the caller's"
                        f" {self.user_byte_limit} byte storage quota"
                    )
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
        observed: StoredObject,
        level: int,
        verified_at: datetime,
        replacement: StoredBytes | None = None,
        retire_after: datetime | None = None,
    ) -> bool:
        """Conditionally stamp one observed layout and report whether this worker won.

        Reaching this point required restoring the object and matching it against the
        stored content hash, so the same transaction records that verification.
        The update compares the complete observed representation and prior compression
        policy. Concurrent or stale workers therefore cannot overwrite a newer layout.
        `content_hash` and `size` describe the original bytes and never move.
        """
        if replacement is not None:
            if (
                replacement.content_hash != observed.content_hash
                or replacement.size != observed.size
            ):
                raise ValueError("a compacted representation must preserve content identity")
            if replacement.key == observed.key:
                raise ValueError("a compacted representation must use a fresh storage key")
            if replacement.stored_size >= observed.stored_size:
                raise ValueError(
                    "a compacted replacement must be smaller than the observed layout"
                )
            if replacement.encoding_level != level:
                raise ValueError("a compacted replacement must record the evaluated policy level")
            if retire_after is None:
                raise ValueError("a compacted replacement must defer its old layout's retirement")
        elif retire_after is not None:
            raise ValueError("an unchanged layout has nothing to retire")

        values: dict[str, str | int | datetime | Blob.Encoding | None] = {
            "encoding_level": level,
            "integrity_checked_at": verified_at,
            "integrity_error": None,
        }
        if replacement is not None:
            values.update(
                storage_key=replacement.key,
                stored_size=replacement.stored_size,
                encoding=replacement.encoding,
                etag=replacement.etag,
                storage_version=replacement.version,
            )
        async with User.system().owner as session:
            written = await session.exec(
                update(Blob)
                .where(*_observed_layout(observed))
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            won = bool(written.rowcount)
            if won and replacement is not None:
                assert retire_after is not None
                session.add(
                    ObjectRetirement(
                        storage_key=observed.key,
                        storage_version=observed.version,
                        stored_size=observed.stored_size,
                        delete_after=retire_after,
                    )
                )
            return won

    async def record_integrity(
        self,
        checks: tuple[IntegrityCheck, ...],
        checked_at: datetime,
    ) -> tuple[IntegrityCheck, ...]:
        """Stamp only results whose exact observed layout is still current."""
        if not checks:
            return ()
        recorded: list[IntegrityCheck] = []
        async with User.system().owner as session:
            for check in checks:
                written = await session.exec(
                    update(Blob)
                    .where(*_observed_layout(check.observed))
                    .values(
                        integrity_checked_at=checked_at,
                        integrity_error=check.error,
                    )
                    .execution_options(synchronize_session=False)
                )
                if written.rowcount:
                    recorded.append(check)
        return tuple(recorded)

    async def claim_retirements(
        self,
        delete_before: datetime,
        lease_until: datetime,
        limit: int,
    ) -> tuple[RetiredObject, ...]:
        """Lease reader-safe obsolete layouts so only one collector deletes each key."""
        async with User.system().owner as session:
            rows = list(
                await session.exec(
                    select(ObjectRetirement)
                    .where(ObjectRetirement.delete_after <= delete_before)
                    .order_by(ObjectRetirement.delete_after, ObjectRetirement.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                row.delete_after = lease_until
        return tuple(
            RetiredObject(
                id=row.id,
                key=row.storage_key,
                version=row.storage_version,
                stored_size=row.stored_size,
                delete_after=row.delete_after,
            )
            for row in rows
        )

    async def retirement_is_unreferenced(self, retired: RetiredObject) -> bool:
        """Refuse deletion if a corrupted or manually edited row reused a retired key."""
        async with User.system().owner as session:
            current = await session.scalar(select(Blob.id).where(Blob.storage_key == retired.key))
            return current is None

    async def forget_retirement(self, retired: RetiredObject) -> bool:
        """Remove one collected retirement only if no current blob refers to its key."""
        async with User.system().owner as session:
            current = await session.scalar(select(Blob.id).where(Blob.storage_key == retired.key))
            if current is not None:
                return False
            removed = await session.exec(
                delete(ObjectRetirement).where(
                    ObjectRetirement.id == retired.id,
                    ObjectRetirement.storage_key == retired.key,
                    ObjectRetirement.delete_after == retired.delete_after,
                )
            )
            return bool(removed.rowcount)

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
                markdown=content.markdown,
                conversion_policy=content.conversion_policy,
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

    async def record_candidate(
        self,
        user: User,
        original: OriginalArtifact,
        markdown: str,
        policy: str,
        error: str | None = None,
    ) -> None:
        """Keep one proposed derivative without replacing findable production text.

        A rejected candidate stays available for diagnosis, while `candidate_policy` is the
        durable cursor that keeps the same policy pass from offering it forever.
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
                .values(
                    candidate_markdown=markdown,
                    candidate_policy=policy,
                    candidate_error=error,
                )
                .execution_options(synchronize_session=False)
            )

    async def record_candidate_error(
        self,
        user: User,
        original: OriginalArtifact,
        policy: str,
        error: str,
    ) -> None:
        """Quarantine a failed attempt while preserving the last good derivative."""
        async with user as session:
            written = await session.exec(
                update(Artifact.Content)
                .where(Artifact.Content.id == original.content_id)
                .values(candidate_policy=policy, candidate_error=error[:1024])
                .execution_options(synchronize_session=False)
            )
            if not written.rowcount:
                raise LookupError("artifact original disappeared before candidate quarantine")

    async def promote_candidate(
        self,
        user: User,
        original: OriginalArtifact,
        policy: str,
        indexed_at: datetime,
        caption_metadata: list[dict[str, JsonValue]],
    ) -> None:
        """Atomically make an indexed candidate the production derivative and clear quarantine."""
        async with user as session:
            written = await session.exec(
                update(Artifact.Content)
                .where(
                    Artifact.Content.id == original.content_id,
                    Artifact.Content.candidate_policy == policy,
                    Artifact.Content.candidate_markdown.is_not(None),
                )
                .values(
                    markdown=Artifact.Content.candidate_markdown,
                    caption_metadata=caption_metadata,
                    conversion_policy=policy,
                    candidate_markdown=None,
                    candidate_policy=None,
                    candidate_error=None,
                    indexed_at=indexed_at,
                )
                .execution_options(synchronize_session=False)
            )
            if not written.rowcount:
                raise LookupError("conversion candidate disappeared before promotion")
