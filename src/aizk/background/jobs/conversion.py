from typing import ClassVar, Protocol

from pydantic import UUID7

from ...config import settings
from ...types import Scopes
from ..enum import JobPriority
from ..queue import Queue, QueueJob
from .models import ArtifactConversionJob


class ArtifactProcessor(Protocol):
    """Turn one durable original into stored and recallable derivatives."""

    async def process(self, content_id: UUID7, scopes: Scopes) -> None:
        """Process one original under its exact queued scopes."""
        ...


class DoclingConversionJob(QueueJob[ArtifactConversionJob]):
    """Convert one queued immutable original through the configured artifact processor."""

    entrypoint: ClassVar[str] = "aizk_convert_artifact"
    payload_type: ClassVar[type[ArtifactConversionJob]] = ArtifactConversionJob
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
