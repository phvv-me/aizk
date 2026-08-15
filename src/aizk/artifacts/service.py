import re
from compression import zstd
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger
from obstore.exceptions import BaseError as ObjectStoreError
from patos import FrozenModel
from pydantic import UUID7, AnyHttpUrl
from sqlalchemy.exc import SQLAlchemyError

from ..background.jobs.projection import enqueue_document
from ..config import settings
from ..extract.ingest import TextIngestor, TextSource
from ..integrations.clamav import ContentScanner
from ..integrations.converter import ArtifactConverter
from ..integrations.docling import (
    ArtifactBytes,
    ArtifactReader,
    DoclingConversionError,
    DoclingOutput,
    DoclingUnreadableFormatError,
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
from .boilerplate import WebBoilerplateCleaner
from .description import ArtifactDescriptionEnricher, CaptionError
from .formats import FormatPolicy
from .models import (
    ArtifactDocument,
    ArtifactReceipt,
    CompactionReport,
    IntegrityReport,
    OriginalArtifact,
    OriginalDescription,
    RetirementReport,
)
from .quality import MarkdownQualityGate
from .repository import ArtifactRepository, StorageQuotaExceeded
from .visual import ArtifactVisualEnricher

_INLINE_LINK = re.compile(r"(?P<prefix>!?\[[^\]\n]*\]\()(?P<destination><[^>\n]+>|[^)\s\n]+)")
_REFERENCE_LINK = re.compile(
    r"^(?P<prefix>[ \t]{0,3}\[[^\]\n]+\]:[ \t]*)(?P<destination><[^>\n]+>|\S+)",
    re.MULTILINE,
)


def _resolve_markdown_links(markdown: str, source_uri: str | None) -> str:
    """Resolve source-relative Markdown destinations against one HTTP source URI."""
    if source_uri is None or urlparse(source_uri).scheme not in {"http", "https"}:
        return markdown

    def replace(match: re.Match[str]) -> str:
        raw = match.group("destination")
        angled = raw.startswith("<") and raw.endswith(">")
        destination = raw[1:-1] if angled else raw
        if urlparse(destination).scheme:
            return match.group(0)
        resolved = urljoin(source_uri, destination)
        prefix = match.group("prefix")
        return f"{prefix}<{resolved}>" if angled else f"{prefix}{resolved}"

    return _REFERENCE_LINK.sub(replace, _INLINE_LINK.sub(replace, markdown))


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
        scanner: ContentScanner,
        storage: ByteStore,
        repository: ArtifactRepository,
        enqueuer: ArtifactEnqueuer,
        formats: FormatPolicy | None = None,
    ) -> None:
        self.reader = reader
        self.scanner = scanner
        self.storage = storage
        self.repository = repository
        self.enqueuer = enqueuer
        self.formats = formats or FormatPolicy()

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
        """Recognize, scan, store, register, and enqueue one bounded artifact.

        The format check runs first and costs nothing, so an artifact aizk cannot read is
        refused before it consumes a malware scan or a byte of storage. The caller resolves
        and authorizes `target` before delivery, so intake writes to exactly those scopes
        under PostgreSQL row security. The whole artifact is held in memory, bounded by the
        declared upload limit, because format recognition, malware scanning, content
        hashing, and object storage each need the complete bytes.
        """
        media_type = self.formats.accept(artifact.media_type, artifact.content)
        annotate_operation(Usage.Event.Operation.remember_file, target)
        await self.scanner.scan(artifact.content)
        stored = await self.storage.put(artifact.content)
        try:
            receipt = await self.repository.create_original(
                user,
                stored,
                OriginalDescription(
                    filename=artifact.filename,
                    media_type=media_type,
                    source_uri=source_uri,
                    companion_text=companion_text,
                    observed_at=observed_at,
                    expires_at=expires_at,
                ),
                target,
            )
        except SQLAlchemyError, StorageQuotaExceeded:
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
        converter: ArtifactConverter,
        storage: ByteStore,
        repository: ArtifactRepository,
        visual: ArtifactVisualEnricher | None = None,
        cleaner: WebBoilerplateCleaner | None = None,
        description: ArtifactDescriptionEnricher | None = None,
        quality: MarkdownQualityGate | None = None,
    ) -> None:
        self.converter = converter
        self.storage = storage
        self.repository = repository
        self.visual = visual
        self.description = description
        self.cleaner = cleaner
        self.quality = quality or MarkdownQualityGate()

    async def process(
        self,
        content_id: UUID7,
        scopes: Scopes,
        policy: str = "converter-v2",
    ) -> None:
        """Convert one original and make its text recallable before marking it ready."""
        user = User.system(scopes)
        original: OriginalArtifact | None = None
        content: bytes | None = None
        candidate_attempted = False
        await self.repository.set_state(
            user,
            content_id,
            scopes,
            Artifact.Content.State.processing,
        )
        try:
            original = await self.repository.original(user, content_id, scopes)
            original, content = await self.read_original(user, original, content_id, scopes)
            response = await self.converter.convert(
                ArtifactBytes(
                    content=content,
                    filename=original.filename,
                    media_type=original.media_type,
                )
            )
            try:
                output = DoclingOutput.from_response(response)
            except DoclingUnreadableFormatError as error:
                await self.reject(
                    user,
                    original,
                    content_id,
                    scopes,
                    content,
                    Artifact.Content.State.unreadable,
                    error,
                    policy,
                )
                return
            except DoclingConversionError as error:
                await self.reject(
                    user,
                    original,
                    content_id,
                    scopes,
                    content,
                    Artifact.Content.State.failed,
                    error,
                    policy,
                )
                return
            markdown = self.declutter(output.markdown, original)
            caption_metadata = []
            if self.description is not None:
                described = await self.description.enrich(original, content, markdown)
                markdown = described.markdown
                caption_metadata = described.metadata()
            assessment = self.quality.assess(original.markdown, markdown)
            await self.repository.record_candidate(
                user,
                original,
                markdown,
                policy,
                assessment.reason,
            )
            if not assessment.accepted:
                await self.repository.set_state(
                    user,
                    content_id,
                    scopes,
                    Artifact.Content.State.ready,
                )
                return

            candidate_attempted = True
            await self.index(
                user,
                original,
                Artifact.Content.State.ready,
                content,
                markdown,
            )
            await self.repository.promote_candidate(
                user,
                original,
                policy,
                datetime.now(UTC),
                caption_metadata,
            )
            await self.repository.set_state(
                user,
                content_id,
                scopes,
                Artifact.Content.State.ready,
            )
        except (
            ByteLimitExceeded,
            CaptionError,
            DoclingConversionError,
            IntegrityMismatch,
            SQLAlchemyError,
            ValueError,
            httpx.HTTPStatusError,
        ) as error:
            if original is not None and original.converted:
                if candidate_attempted and content is not None:
                    await self.index(
                        user,
                        original,
                        Artifact.Content.State.ready,
                        content,
                        original.markdown,
                    )
                await self.repository.record_candidate_error(
                    user,
                    original,
                    policy,
                    str(error),
                )
                await self.repository.set_state(
                    user,
                    content_id,
                    scopes,
                    Artifact.Content.State.ready,
                )
            else:
                await self.repository.set_state(
                    user,
                    content_id,
                    scopes,
                    Artifact.Content.State.failed,
                    str(error)[:1024],
                )
            raise

    async def read_original(
        self,
        user: User,
        original: OriginalArtifact,
        content_id: UUID7,
        scopes: Scopes,
    ) -> tuple[OriginalArtifact, bytes]:
        """Retry through the current pointer if compaction retired a stale layout."""
        try:
            content = await self.storage.get(
                original.storage_key,
                encoding=original.storage_encoding,
                expected_size=original.size,
                expected_hash=original.storage_hash,
                version=original.storage_version,
            )
            return original, content
        except ObjectStoreError, OSError:
            current = await self.repository.original(user, content_id, scopes)
            observed = (
                original.storage_key,
                original.storage_version,
                original.storage_encoding,
            )
            refreshed = (
                current.storage_key,
                current.storage_version,
                current.storage_encoding,
            )
            if refreshed == observed:
                raise
            content = await self.storage.get(
                current.storage_key,
                encoding=current.storage_encoding,
                expected_size=current.size,
                expected_hash=current.storage_hash,
                version=current.storage_version,
            )
            return current, content

    async def reject(
        self,
        user: User,
        original: OriginalArtifact,
        content_id: UUID7,
        scopes: Scopes,
        content: bytes,
        state: ArtifactContent.State,
        error: DoclingConversionError,
        policy: str,
    ) -> None:
        """Keep one metadata-only document recallable and stamp Docling's final verdict.

        `state` tells the caller whether this original stays in the retry pool (`failed`) or
        leaves it for good (`unreadable`), and the stored `error` keeps Docling's own reason
        visible either way.
        """
        if original.converted:
            await self.repository.record_candidate_error(
                user,
                original,
                policy,
                str(error),
            )
            await self.repository.set_state(
                user,
                content_id,
                scopes,
                Artifact.Content.State.ready,
            )
            return
        await self.index(user, original, state, content)
        await self.repository.set_state(user, content_id, scopes, state, str(error))

    def declutter(self, markdown: str, original: OriginalArtifact) -> str:
        """Resolve source-relative links and strip web chrome before the text becomes chunks.

        Order matters here, since resolving first lets the cleaner read a menu's own site off
        destinations the page wrote as relative paths.
        """
        resolved = _resolve_markdown_links(markdown, original.source_uri)
        if self.cleaner is None:
            return resolved
        return self.cleaner.clean_page(resolved, original.media_type, original.source_uri)

    async def index(
        self,
        user: User,
        original: OriginalArtifact,
        state: ArtifactContent.State,
        content: bytes,
        markdown: str | None = None,
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


class ArtifactReindexer:
    """Re-split and re-embed one converted original from the Markdown already in PostgreSQL.

    Conversion has two halves and only the first one is expensive. Docling reads the bytes,
    runs OCR and writes Markdown, then chunking, embedding and graph projection turn that
    Markdown into something recallable. A chunk size change, a lexical prefix change or a new
    embedder invalidates the second half alone, so this pass replays it from the stored text
    and never asks Docling to read the original again. That is what keeps `markdown` a load
    bearing column rather than a derivative nothing consumes.
    """

    def __init__(self, repository: ArtifactRepository) -> None:
        self.repository = repository

    async def reindex(self, content_id: UUID7, scopes: Scopes) -> None:
        """Rebuild one revision's chunks, queue their projection, and stamp the pass."""
        user = User.system(scopes)
        converted = await self.repository.converted(user, content_id, scopes)
        source = ArtifactDocument(
            filename=converted.filename,
            media_type=converted.media_type,
            size=converted.size,
            source_uri=converted.source_uri,
            companion_text=converted.companion_text,
            markdown=converted.markdown,
            conversion_state=Artifact.Content.State.ready,
        )
        document_id = await TextIngestor(user).rechunk(
            TextSource(
                text=await source.to_markdown(),
                title=converted.filename,
                source_uri=converted.source_uri,
                artifact_id=converted.artifact_id,
                artifact_content_id=converted.content_id,
                original_content_hash=converted.storage_hash,
                created_by=converted.created_by,
                scopes=scopes,
                capture=CaptureContext(
                    observed_at=converted.observed_at,
                    expires_at=converted.expires_at,
                ),
            )
        )
        if document_id is None:
            raise DoclingConversionError("stored Markdown re-chunked into no document")
        await self.repository.record_indexing(user, content_id, datetime.now(UTC))
        await enqueue_document(document_id, scopes)


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
        recorded = await self.repository.record_integrity(checks, checked_at)
        failed = sum(check.error is not None for check in recorded)
        return IntegrityReport(
            checked=len(recorded),
            valid=len(recorded) - failed,
            failed=failed,
        )

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
            return IntegrityCheck(observed=stored, error=message)
        return IntegrityCheck(observed=stored)


class CompactionDisabled(RuntimeError):
    """Compaction was asked to re-lay objects while compression is turned off."""


class CompactionOutcome(FrozenModel):
    """What one object costs the store after the compaction pass looked at it."""

    stored_size: int
    rewritten: bool = False
    conflicted: bool = False
    retired_bytes: int = 0
    error: str | None = None


class ArtifactCompaction:
    """Re-lay stored objects under the current compression policy, byte for byte identical.

    Raising `object_store_compression_level` only changes how new objects are written, so
    everything accepted under a weaker policy, or before compression existed at all, keeps
    its old layout until this pass walks it. Nothing here is lossy. Every object is restored
    and matched against the content hash before it is written again, and the hash, the size,
    and therefore `ArtifactIntegrity` all continue to describe the original bytes.
    """

    def __init__(self, storage: ByteStore, repository: ArtifactRepository) -> None:
        self.storage = storage
        self.repository = repository

    async def compact(self, limit: int) -> CompactionReport:
        """Re-lay one bounded batch and report active savings plus deferred retirements."""
        if not self.storage.compression_enabled:
            raise CompactionDisabled(
                "object store compression is turned off, so there is no policy to compact toward"
            )
        level = self.storage.compression_level
        candidates = await self.repository.compaction_candidates(level, limit)
        outcomes = [await self.rewrite(stored, level) for stored in candidates]
        return CompactionReport(
            examined=len(outcomes),
            rewritten=sum(outcome.rewritten for outcome in outcomes),
            conflicted=sum(outcome.conflicted for outcome in outcomes),
            failed=sum(outcome.error is not None for outcome in outcomes),
            stored_bytes_before=sum(stored.stored_size for stored in candidates),
            stored_bytes_after=sum(outcome.stored_size for outcome in outcomes),
            pending_retirement_bytes=sum(outcome.retired_bytes for outcome in outcomes),
        )

    async def rewrite(self, stored: StoredObject, level: int) -> CompactionOutcome:
        """Restore, re-encode, and repoint one object, keeping whichever layout is denser.

        The replacement lands under a fresh key. PostgreSQL atomically repoints the blob and
        records when the old key becomes safe to delete. Nightly cleanup waits beyond every
        signed URL lifetime before removing it, so a reader holding the observed pointer
        remains valid across the swap. A failure is left unstamped and reported, which keeps
        the object a candidate and lets the integrity pass raise it separately.
        """
        try:
            data = await self.storage.get(
                stored.key,
                encoding=stored.encoding,
                expected_size=stored.size,
                expected_hash=stored.content_hash,
                version=stored.version,
            )
            replacement = await self.storage.put(data)
        except (
            ByteLimitExceeded,
            IntegrityMismatch,
            ObjectStoreError,
            OSError,
            zstd.ZstdError,
        ) as error:
            message = f"{type(error).__name__}: {error}"[:1024]
            logger.error("artifact compaction failure blob={} error={}", stored.id, message)
            return CompactionOutcome(stored_size=stored.stored_size, error=message)
        verified_at = datetime.now(UTC)
        if replacement.stored_size >= stored.stored_size:
            await self.storage.delete(replacement.key)
            won = await self.repository.record_compaction(stored, level, verified_at)
            if not won:
                return CompactionOutcome(
                    stored_size=stored.stored_size,
                    conflicted=True,
                )
            return CompactionOutcome(stored_size=stored.stored_size)
        won = await self.repository.record_compaction(
            stored,
            level,
            verified_at,
            replacement,
            retire_after=verified_at + self.storage.retirement_grace,
        )
        if won:
            return CompactionOutcome(
                stored_size=replacement.stored_size,
                rewritten=True,
                retired_bytes=stored.stored_size,
            )
        await self.storage.delete(replacement.key)
        return CompactionOutcome(stored_size=stored.stored_size, conflicted=True)


class ArtifactRetirement:
    """Delete obsolete layouts only after stale readers and signed URLs have expired."""

    def __init__(self, storage: ByteStore, repository: ArtifactRepository) -> None:
        self.storage = storage
        self.repository = repository

    async def collect(self, limit: int) -> RetirementReport:
        """Lease and delete one bounded batch while retaining failures for retry."""
        now = datetime.now(UTC)
        retired = await self.repository.claim_retirements(
            now,
            now + timedelta(minutes=5),
            limit,
        )
        deleted = 0
        failed = 0
        reclaimed = 0
        for item in retired:
            if not await self.repository.retirement_is_unreferenced(item):
                logger.error("retired artifact object is referenced key={}", item.key)
                failed += 1
                continue
            try:
                await self.storage.delete(item.key)
            except (ObjectStoreError, OSError) as error:
                logger.error(
                    "artifact object retirement failure key={} error={}",
                    item.key,
                    f"{type(error).__name__}: {error}"[:1024],
                )
                failed += 1
                continue
            if await self.repository.forget_retirement(item):
                deleted += 1
                reclaimed += item.stored_size
        return RetirementReport(
            examined=len(retired),
            deleted=deleted,
            failed=failed,
            reclaimed=reclaimed,
        )
