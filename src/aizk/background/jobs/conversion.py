from typing import ClassVar, Protocol
from uuid import UUID

from patos import FrozenModel
from pydantic import UUID7
from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import select, update

from ...config import settings
from ...store.identity import User
from ...store.models.tables import Artifact, ArtifactContent, Blob
from ...types import Scopes
from ..enum import JobPriority
from ..queue import Queue, QueueJob, QueuePayload
from .models import ArtifactConversionJob


class ArtifactProcessor(Protocol):
    """Turn one durable original into stored and recallable derivatives."""

    async def process(self, content_id: UUID7, scopes: Scopes) -> None:
        """Process one original under its exact queued scopes."""
        ...


class DoclingConversionJob(QueueJob[ArtifactConversionJob]):
    """Convert one queued immutable original through the configured artifact processor."""

    entrypoint: ClassVar[str] = "aizk_convert_artifact"
    payload_type: ClassVar[type[QueuePayload]] = ArtifactConversionJob
    priority: ClassVar[int] = JobPriority.artifact
    concurrency_limit: ClassVar[int] = settings.docling_concurrency

    def __init__(self, processor: ArtifactProcessor) -> None:
        self.processor = processor

    async def handle(self, payload: ArtifactConversionJob) -> None:
        """Resolve and process one original only through its durable PostgreSQL identity."""
        await self.processor.process(payload.artifact_content_id, payload.scopes)


class ArtifactQueue:
    """Enqueue conversion IDs through PgQueuer without carrying files or source URIs."""

    def __init__(self, job: DoclingConversionJob) -> None:
        self.job = job

    async def enqueue(self, content_id: UUID7, scopes: Scopes) -> bool:
        """Persist one deduplicated conversion request."""
        async with Queue(dsn=settings.asyncpg_dsn) as queue:
            admitted = await self.job.enqueue(
                queue,
                ArtifactConversionJob(
                    artifact_content_id=content_id,
                    scopes=scopes,
                ),
                str(content_id),
            )
            if not admitted:
                await queue.requeue_failed(type(self.job))
            return admitted


class ArtifactRecovery:
    """Reconcile retained queue failures and orphaned durable conversion failures."""

    async def retry(self, limit: int) -> int:
        """Recover at most `limit` conversions, prioritizing retained queue jobs."""
        if limit < 1:
            raise ValueError("conversion retry limit must be positive")
        async with Queue(dsn=settings.asyncpg_dsn) as queue:
            requeued = await queue.requeue_failed(DoclingConversionJob, limit)
            return requeued + await self.enqueue_orphans(queue, limit - requeued)

    async def enqueue_orphans(self, queue: Queue, limit: int) -> int:
        """Enqueue durable failures without a live job and update only admitted rows."""
        if limit == 0:
            return 0
        active_ids = await self.active_content_ids(queue)
        async with User.system().owner as session:
            rows = (
                await session.exec(
                    select(ArtifactContent)
                    .where(
                        ArtifactContent.state == ArtifactContent.State.failed,
                        ArtifactContent.id.not_in(active_ids),
                    )
                    .order_by(ArtifactContent.updated_at, ArtifactContent.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            admitted = 0
            for row in rows:
                if await queue.enqueue(
                    DoclingConversionJob,
                    ArtifactConversionJob(
                        artifact_content_id=row.id,
                        scopes=frozenset(row.scopes),
                    ),
                    str(row.id),
                ):
                    row.state = ArtifactContent.State.queued
                    row.error = None
                    row.processed_at = None
                    admitted += 1
            return admitted

    async def active_content_ids(self, queue: Queue) -> tuple[UUID, ...]:
        """Decode content IDs currently protected by PgQueuer deduplication."""
        payloads = await queue.active_payloads(DoclingConversionJob.entrypoint)
        content_ids: list[UUID] = []
        for payload in payloads:
            try:
                content_ids.append(ArtifactConversionJob.decode(payload).artifact_content_id)
            except TypeError, ValueError:
                continue
        return tuple(content_ids)


class ReconversionSweep(FrozenModel):
    """Which already converted originals one reconversion pass offers to the queue again.

    A conversion policy change reaches stored text only by converting the original again, and
    each change touches a different family of originals. Web chrome lives in fetched HTML, and
    an OCR language reaches anything Docling reads with an OCR engine.
    """

    media_prefixes: tuple[str, ...]
    source_prefix: str | None = None

    def selects(self) -> ColumnElement[bool]:
        """The predicate matching this sweep's originals inside one converted revision."""
        media = or_(*[Blob.media_type.startswith(prefix) for prefix in self.media_prefixes])
        if self.source_prefix is None:
            return media
        return and_(media, Artifact.source_uri.startswith(self.source_prefix))


class ArtifactReconversion:
    """Offer already converted originals to the conversion queue again, oldest conversion first."""

    def __init__(self, sweep: ReconversionSweep) -> None:
        self.sweep = sweep

    async def enqueue(self, limit: int) -> int:
        """Enqueue at most `limit` of this sweep's converted originals.

        The ordering invariant is that an original's move back to `queued` commits before its
        task exists and never after. A worker can pick the task up the instant it lands and
        write `ready` again, so a sweep still holding an open transaction would overwrite that
        worker with a stale `queued` and leave an artifact nothing is working on. Reading the
        whole window first also keeps a row unlocked while the queue writes.
        """
        if limit < 1:
            raise ValueError("reconversion limit must be positive")
        admitted = 0
        async with Queue(dsn=settings.asyncpg_dsn) as queue:
            for content_id, scopes in await self.originals(limit):
                await self.claim(content_id)
                admitted += await queue.enqueue(
                    DoclingConversionJob,
                    ArtifactConversionJob(artifact_content_id=content_id, scopes=scopes),
                    str(content_id),
                )
        return admitted

    async def originals(self, limit: int) -> tuple[tuple[UUID7, Scopes], ...]:
        """Read the converted originals still carrying an older conversion, oldest first."""
        async with User.system().owner as session:
            rows = (
                await session.exec(
                    select(ArtifactContent)
                    .join(Artifact, Artifact.id == ArtifactContent.artifact_id)
                    .join(Blob, Blob.id == ArtifactContent.blob_id)
                    .where(
                        ArtifactContent.state == ArtifactContent.State.ready,
                        self.sweep.selects(),
                    )
                    .order_by(ArtifactContent.updated_at, ArtifactContent.id)
                    .limit(limit)
                )
            ).all()
            return tuple((row.id, frozenset(row.scopes)) for row in rows)

    async def claim(self, content_id: UUID7) -> None:
        """Commit one original's move back to `queued` in its own transaction."""
        async with User.system().owner as session:
            await session.exec(
                update(ArtifactContent)
                .where(ArtifactContent.id == content_id)
                .values(state=ArtifactContent.State.queued)
                .execution_options(synchronize_session=False)
            )


# Fetched HTML is where site chrome lives, and the boilerplate cleaner only runs on those.
_WEB_PAGES = ReconversionSweep(
    media_prefixes=("text/html", "application/xhtml+xml"),
    source_prefix="http",
)
# Anything Docling may read with an OCR engine, whatever door it arrived through.
_SCANNED_DOCUMENTS = ReconversionSweep(media_prefixes=("application/pdf", "image/"))


async def retry_failed_artifacts(limit: int = 100) -> int:
    """Recover retained and orphaned durable failures within one total budget."""
    return await ArtifactRecovery().retry(limit)


async def reconvert_web_pages(limit: int = 100) -> int:
    """Requeue converted web pages so stored chrome leaves their text on the next pass."""
    return await ArtifactReconversion(_WEB_PAGES).enqueue(limit)


async def reconvert_scanned_documents(limit: int = 100) -> int:
    """Requeue what OCR read, so a corrected engine and language rewrite their text."""
    return await ArtifactReconversion(_SCANNED_DOCUMENTS).enqueue(limit)
