from compression import zstd
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import httpx
from loguru import logger
from obstore.exceptions import BaseError as ObjectStoreError
from pydantic import UUID7, AnyHttpUrl, JsonValue
from sqlalchemy.exc import SQLAlchemyError

from ..background.jobs.projection import enqueue_document
from ..config import settings
from ..extract.ingest import TextIngestor, TextSource
from ..integrations.clamav import ClamAVClient
from ..integrations.docling import (
    ArtifactBytes,
    ArtifactReader,
    DoclingClient,
    DoclingConversionError,
    DoclingOutput,
    URISource,
)
from ..provenance import CaptureContext
from ..storage import (
    ByteLimitExceeded,
    ByteStore,
    IntegrityCheck,
    IntegrityMismatch,
    StoredObject,
)
from ..store import Artifact, Usage
from ..store.identity import User
from ..store.models.tables import ArtifactContent
from ..types import ScopeNames, Scopes
from ..usage import annotate_operation
from .models import (
    ArtifactDocument,
    ArtifactReceipt,
    IntegrityReport,
    OriginalArtifact,
    OriginalDescription,
)
from .repository import ArtifactRepository
from .visual import ArtifactVisualEnricher


class ArtifactEnqueuer(Protocol):
    """Persist one conversion request after the original metadata commits."""

    async def enqueue(self, content_id: UUID7, scopes: Scopes) -> bool:
        """Enqueue one exact original once."""
        ...


class ArtifactIntake:
    """Accept one upload or URI after scope authorization, bounded reading, and malware scan."""

    def __init__(
        self,
        reader: ArtifactReader,
        scanner: ClamAVClient,
        storage: ByteStore,
        repository: ArtifactRepository,
        enqueuer: ArtifactEnqueuer,
    ) -> None:
        self.reader = reader
        self.scanner = scanner
        self.storage = storage
        self.repository = repository
        self.enqueuer = enqueuer

    async def uri(
        self,
        user: User,
        uri: str,
        scopes: ScopeNames | None = None,
        companion_text: str | None = None,
        observed_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> ArtifactReceipt:
        """Fetch one public HTTPS resource once before accepting its immutable bytes."""
        target = user.write_scope(scopes)
        source = URISource(uri=cast("AnyHttpUrl", uri))
        artifact = await self.reader.read_uri(source)
        return await self.accept(
            user,
            artifact,
            source_uri=str(source.uri),
            target=target,
            companion_text=companion_text,
            observed_at=observed_at,
            expires_at=expires_at,
        )

    async def accept(
        self,
        user: User,
        artifact: ArtifactBytes,
        *,
        target: Scopes,
        source_uri: str | None = None,
        companion_text: str | None = None,
        observed_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> ArtifactReceipt:
        """Scan, store, register, and enqueue one bounded artifact without temporary files.

        The caller resolves and authorizes `target` before delivery, so intake writes to
        exactly those scopes under PostgreSQL row security. The whole artifact is held in
        memory, bounded by the declared upload limit, because malware scanning, content
        hashing, and object storage each need the complete bytes.
        """
        annotate_operation(Usage.Event.Operation.remember_file, target)
        await self.scanner.scan(artifact.content)
        stored = await self.storage.put(artifact.content)
        try:
            receipt = await self.repository.create_original(
                user,
                stored,
                OriginalDescription(
                    filename=artifact.filename,
                    media_type=artifact.media_type,
                    source_uri=source_uri,
                    companion_text=companion_text,
                    observed_at=observed_at,
                    expires_at=expires_at,
                ),
                target,
            )
        except SQLAlchemyError:
            await self.storage.delete(stored.key)
            raise
        await self.enqueuer.enqueue(receipt.content_id, target)
        await self.repository.set_state(
            user,
            receipt.content_id,
            target,
            Artifact.Content.State.queued,
        )
        return receipt.model_copy(update={"state": Artifact.Content.State.queued})

    async def dispatch_pending(
        self,
        scopes: Scopes,
        limit: int = settings.artifact_dispatch_batch_size,
    ) -> int:
        """Recover originals left pending by a queue or process interruption."""
        user = User.system(scopes)
        content_ids = await self.repository.pending(user, scopes, limit)
        for content_id in content_ids:
            await self.enqueuer.enqueue(content_id, scopes)
            await self.repository.set_state(
                user,
                content_id,
                scopes,
                Artifact.Content.State.queued,
            )
        return len(content_ids)


class ArtifactProcessor:
    """Materialize, convert, persist, and ingest one durable queued original."""

    def __init__(
        self,
        converter: DoclingClient,
        storage: ByteStore,
        repository: ArtifactRepository,
        visual: ArtifactVisualEnricher | None = None,
    ) -> None:
        self.converter = converter
        self.storage = storage
        self.repository = repository
        self.visual = visual

    async def process(self, content_id: UUID7, scopes: Scopes) -> None:
        """Convert one original and make its text recallable before marking it ready."""
        user = User.system(scopes)
        await self.repository.set_state(
            user,
            content_id,
            scopes,
            Artifact.Content.State.processing,
        )
        try:
            original = await self.repository.original(user, content_id, scopes)
            content = await self.storage.get(
                original.storage_key,
                encoding=original.storage_encoding,
                expected_size=original.size,
                expected_hash=original.storage_hash,
                version=original.storage_version,
            )
            response = await self.converter.convert(
                ArtifactBytes(
                    content=content,
                    filename=original.filename,
                    media_type=original.media_type,
                )
            )
            try:
                output = DoclingOutput.from_response(response)
            except DoclingConversionError as error:
                await self.index(
                    user,
                    original,
                    Artifact.Content.State.failed,
                    content,
                )
                await self.repository.set_state(
                    user,
                    content_id,
                    scopes,
                    Artifact.Content.State.failed,
                    str(error),
                )
                return
            await self.repository.store_conversion(
                user,
                original,
                output.markdown,
                output.docling_json,
                output.details,
            )
            await self.index(
                user,
                original,
                Artifact.Content.State.ready,
                content,
                output.markdown,
                output.details,
            )
            await self.repository.set_state(
                user,
                content_id,
                scopes,
                Artifact.Content.State.ready,
            )
        except (
            ByteLimitExceeded,
            DoclingConversionError,
            IntegrityMismatch,
            httpx.HTTPStatusError,
        ) as error:
            await self.repository.set_state(
                user,
                content_id,
                scopes,
                Artifact.Content.State.failed,
                str(error),
            )
            raise

    async def index(
        self,
        user: User,
        original: OriginalArtifact,
        state: ArtifactContent.State,
        content: bytes,
        markdown: str | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> None:
        """Make a converted or metadata-only original recallable as one stable document."""
        source = ArtifactDocument(
            filename=original.filename,
            media_type=original.media_type,
            size=original.size,
            source_uri=original.source_uri,
            companion_text=original.companion_text,
            markdown=markdown,
            conversion_state=state,
            details=details or {},
        )
        document_id, _ = await TextIngestor(user).ingest(
            TextSource(
                text=await source.to_markdown(),
                title=original.filename,
                source_uri=original.source_uri,
                artifact_id=original.artifact_id,
                artifact_content_id=original.content_id,
                original_content_hash=original.storage_hash,
                created_by=original.created_by,
                scopes=original.scopes,
                capture=CaptureContext(
                    observed_at=original.observed_at,
                    expires_at=original.expires_at,
                ),
            )
        )
        if document_id is None:
            raise DoclingConversionError("artifact metadata did not create a document")
        if self.visual is not None and self.visual.supports(original.media_type):
            await self.visual.enrich(user, document_id, original, content)
        if source.semantic:
            await enqueue_document(document_id, original.scopes)


class ArtifactIntegrity:
    """Verify immutable originals incrementally through their existing storage contract."""

    def __init__(self, storage: ByteStore, repository: ArtifactRepository) -> None:
        self.storage = storage
        self.repository = repository

    async def verify(self, limit: int, interval_days: int) -> IntegrityReport:
        """Verify one stale batch and persist each result for health reporting and retries."""
        checked_at = datetime.now(UTC)
        objects = await self.repository.integrity_candidates(
            checked_at - timedelta(days=interval_days),
            limit,
        )
        checks = tuple([await self.check(stored) for stored in objects])
        await self.repository.record_integrity(checks, checked_at)
        failed = sum(check.error is not None for check in checks)
        return IntegrityReport(checked=len(checks), valid=len(checks) - failed, failed=failed)

    async def check(self, stored: StoredObject) -> IntegrityCheck:
        """Read, decode, bound, and compare one object without exposing its storage key."""
        try:
            await self.storage.get(
                stored.key,
                encoding=stored.encoding,
                expected_size=stored.size,
                expected_hash=stored.content_hash,
                version=stored.version,
            )
        except (
            ByteLimitExceeded,
            IntegrityMismatch,
            ObjectStoreError,
            OSError,
            zstd.ZstdError,
        ) as error:
            message = f"{type(error).__name__}: {error}"[:1024]
            logger.error("artifact integrity failure blob={} error={}", stored.id, message)
            return IntegrityCheck(id=stored.id, error=message)
        return IntegrityCheck(id=stored.id)
