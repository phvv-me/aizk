from typing import ClassVar, Protocol
from uuid import UUID

from loguru import logger
from patos import FrozenModel
from pydantic import UUID7
from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import select

from ...config import settings
from ...store.identity import User
from ...store.models.tables import Artifact, ArtifactContent, Blob
from ...types import Scopes
from ..enum import JobPriority
from ..queue import Queue, QueueJob, QueuePayload
from .models import ArtifactConversionJob, ArtifactReindexJob


class ArtifactProcessor(Protocol):
    """Turn one durable original into stored and findable derivatives."""

    async def process(self, content_id: UUID7, scopes: Scopes, policy: str) -> None:
        """Process one original under its exact queued scopes."""
        ...


class ArtifactReindexer(Protocol):
    """Rebuild one already converted original's chunks from its stored Markdown."""

    async def reindex(self, content_id: UUID7, scopes: Scopes) -> None:
        """Re-chunk one original under its exact queued scopes."""
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
        await self.processor.process(
            payload.artifact_content_id,
            payload.scopes,
            payload.policy,
        )


class MarkdownReindexJob(QueueJob[ArtifactReindexJob]):
    """Re-chunk one queued original through the configured reindexer.

    It runs at the same priority as a conversion because it is the tail of the same
    pipeline, and under the same concurrency limit because a corpus-wide sweep would
    otherwise point every worker at the embedder at once.
    """

    entrypoint: ClassVar[str] = "aizk_reindex_artifact"
    payload_type: ClassVar[type[QueuePayload]] = ArtifactReindexJob
    priority: ClassVar[int] = JobPriority.artifact
    concurrency_limit: ClassVar[int] = settings.docling_concurrency

    def __init__(self, reindexer: ArtifactReindexer) -> None:
        self.reindexer = reindexer

    async def handle(self, payload: ArtifactReindexJob) -> None:
        """Re-chunk one original only through its durable PostgreSQL identity."""
        await self.reindexer.reindex(payload.artifact_content_id, payload.scopes)


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
            except (TypeError, ValueError) as error:
                logger.warning(
                    "ignored malformed active conversion payload of type {}",
                    type(error).__name__,
                )
        return tuple(content_ids)


class ReconversionSweep(FrozenModel):
    """Which already converted originals one reconversion pass offers to the queue again.

    A conversion policy change reaches stored text only by converting the original again, and
    each change touches a different family of originals. Web chrome lives in fetched HTML, and
    an OCR language reaches anything Docling reads with an OCR engine.
    """

    media_prefixes: tuple[str, ...]
    policy: str
    source_prefix: str | None = None
    text_pattern: str | None = None

    def selects(self) -> ColumnElement[bool]:
        """The predicate matching this sweep's originals inside one converted revision."""
        media = or_(*[Blob.media_type.startswith(prefix) for prefix in self.media_prefixes])
        selected: ColumnElement[bool] = media
        if self.source_prefix is not None:
            selected = and_(selected, Artifact.source_uri.startswith(self.source_prefix))
        if self.text_pattern is not None:
            selected = and_(
                selected,
                or_(
                    Artifact.name.op("~")(self.text_pattern),
                    ArtifactContent.markdown.op("~")(self.text_pattern),
                ),
            )
        return selected


class ArtifactReconversion:
    """Offer already converted originals to the conversion queue again, oldest conversion first."""

    def __init__(self, sweep: ReconversionSweep) -> None:
        self.sweep = sweep

    async def enqueue(self, limit: int) -> int:
        """Enqueue at most `limit` of this sweep's converted originals.

        Production stays `ready` while the candidate runs. Queue deduplication and the policy
        stamps make a repeated sweep finite without exposing a half-finished derivative.
        """
        if limit < 1:
            raise ValueError("reconversion limit must be positive")
        admitted = 0
        async with Queue(dsn=settings.asyncpg_dsn) as queue:
            active_ids = await self.active_content_ids(queue)
            for content_id, scopes in await self.originals(limit, active_ids):
                admitted += await queue.enqueue(
                    DoclingConversionJob,
                    ArtifactConversionJob(
                        artifact_content_id=content_id,
                        scopes=scopes,
                        policy=self.sweep.policy,
                    ),
                    str(content_id),
                )
        return admitted

    async def originals(
        self,
        limit: int,
        active_ids: tuple[UUID, ...] = (),
    ) -> tuple[tuple[UUID7, Scopes], ...]:
        """Read the converted originals still carrying an older conversion, oldest first.

        Only the identity and the scope set are selected, since a candidate whose whole row
        came back would detoast the very Markdown this sweep is about to replace.
        """
        predicates = [
            ArtifactContent.state == ArtifactContent.State.ready,
            ArtifactContent.markdown.is_not(None),
            or_(
                ArtifactContent.conversion_policy.is_(None),
                ArtifactContent.conversion_policy != self.sweep.policy,
            ),
            or_(
                ArtifactContent.candidate_policy.is_(None),
                ArtifactContent.candidate_policy != self.sweep.policy,
            ),
            self.sweep.selects(),
        ]
        if active_ids:
            predicates.append(ArtifactContent.id.not_in(active_ids))
        async with User.system().owner as session:
            rows = (
                await session.exec(
                    select(ArtifactContent.id, ArtifactContent.scopes)
                    .join(Artifact, Artifact.id == ArtifactContent.artifact_id)
                    .join(Blob, Blob.id == ArtifactContent.blob_id)
                    .where(*predicates)
                    .order_by(ArtifactContent.updated_at, ArtifactContent.id)
                    .limit(limit)
                )
            ).all()
            return tuple((content_id, frozenset(scopes)) for content_id, scopes in rows)

    async def active_content_ids(self, queue: Queue) -> tuple[UUID, ...]:
        """Decode conversions already protected by the queue's content-ID lock."""
        payloads = await queue.active_payloads(DoclingConversionJob.entrypoint)
        content_ids: list[UUID] = []
        for payload in payloads:
            try:
                content_ids.append(ArtifactConversionJob.decode(payload).artifact_content_id)
            except (TypeError, ValueError) as error:
                logger.warning(
                    "ignored malformed active conversion payload of type {}",
                    type(error).__name__,
                )
        return tuple(content_ids)


class ArtifactRechunk:
    """Offer converted originals to the re-chunk queue, least recently indexed first.

    `indexed_at` is what makes a repeated pass walk forward. A finished job stamps it, so the
    window rotates instead of returning the same head, and a revision converted before the
    column existed reads as null and is swept first.
    """

    async def enqueue(self, limit: int) -> int:
        """Enqueue at most `limit` converted originals for re-chunking.

        Nothing is claimed here. The original keeps its `ready` state throughout because its
        conversion is still current, and it is only the chunks beneath it that are rebuilt, so
        deduplication on the content ID is the whole guard against queueing one twice.
        """
        if limit < 1:
            raise ValueError("re-chunk limit must be positive")
        admitted = 0
        async with Queue(dsn=settings.asyncpg_dsn) as queue:
            for content_id, scopes in await self.converted(limit):
                admitted += await queue.enqueue(
                    MarkdownReindexJob,
                    ArtifactReindexJob(artifact_content_id=content_id, scopes=scopes),
                    str(content_id),
                )
        return admitted

    async def converted(self, limit: int) -> tuple[tuple[UUID7, Scopes], ...]:
        """Read converted originals that still hold Markdown, least recently indexed first.

        The Markdown itself is tested for presence and never selected, so choosing the window
        costs no detoasting at all. The worker reads the text for the one row it works on.
        """
        async with User.system().owner as session:
            rows = (
                await session.exec(
                    select(ArtifactContent.id, ArtifactContent.scopes)
                    .where(
                        ArtifactContent.state == ArtifactContent.State.ready,
                        ArtifactContent.markdown.is_not(None),
                    )
                    .order_by(
                        ArtifactContent.indexed_at.asc().nulls_first(),
                        ArtifactContent.id,
                    )
                    .limit(limit)
                )
            ).all()
            return tuple((content_id, frozenset(scopes)) for content_id, scopes in rows)


# Fetched HTML is where site chrome lives, and the boilerplate cleaner only runs on those.
_WEB_PAGES = ReconversionSweep(
    media_prefixes=("text/html", "application/xhtml+xml"),
    policy="web-boilerplate-v1",
    source_prefix="http",
)
# Japanese PDF and image derivatives are the bounded population at risk from the OCR fix.
_SCANNED_DOCUMENTS = ReconversionSweep(
    media_prefixes=("application/pdf", "image/"),
    policy="japanese-ocr-v2",
    text_pattern="[一-龯ぁ-ゖァ-ヺ々〆ヶ]",
)


async def retry_failed_artifacts(limit: int = 100) -> int:
    """Recover retained and orphaned durable failures within one total budget."""
    return await ArtifactRecovery().retry(limit)


async def reconvert_web_pages(limit: int = 100) -> int:
    """Requeue converted web pages so stored chrome leaves their text on the next pass."""
    return await ArtifactReconversion(_WEB_PAGES).enqueue(limit)


async def reconvert_scanned_documents(limit: int = 100) -> int:
    """Requeue what OCR read, so a corrected engine and language rewrite their text."""
    return await ArtifactReconversion(_SCANNED_DOCUMENTS).enqueue(limit)


async def rechunk_artifacts(limit: int = 100) -> int:
    """Requeue converted originals so a new chunk, lexical or embedding policy reaches them."""
    return await ArtifactRechunk().enqueue(limit)
