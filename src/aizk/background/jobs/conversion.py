from typing import ClassVar, Protocol
from uuid import UUID

from pydantic import UUID7
from sqlmodel import select

from ...config import settings
from ...store.identity import User
from ...store.models.tables import ArtifactContent
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
        names = queue.queries.qbe.settings
        rows = await queue.connection.fetch(
            f"""
            SELECT payload
            FROM {names.queue_table}
            WHERE entrypoint = $1
              AND status IN ('queued', 'picked', 'failed')
              AND payload IS NOT NULL
            """,
            DoclingConversionJob.entrypoint,
        )
        content_ids: list[UUID] = []
        for row in rows:
            try:
                content_ids.append(
                    ArtifactConversionJob.decode(row["payload"]).artifact_content_id
                )
            except TypeError, ValueError:
                continue
        return tuple(content_ids)


async def retry_failed_artifacts(limit: int = 100) -> int:
    """Recover retained and orphaned durable failures within one total budget."""
    return await ArtifactRecovery().retry(limit)
