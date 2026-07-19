from collections.abc import Sequence
from typing import ClassVar

from loguru import logger
from pydantic import UUID7

from ...config import settings
from ...graph.build import GraphClients, extract_and_consolidate, pending_chunks
from ...store import Chunk, Watermark
from ...store.identity import User
from ...types import Scopes
from ..enum import JobPriority
from ..queue import Queue, QueueJob, QueuePayload
from .models import ChunkJob


class ChunkProjectionJob(QueueJob[ChunkJob]):
    """Build one chunk graph projection and mark its touched profiles dirty."""

    entrypoint: ClassVar[str] = "aizk_build_graph_chunk"
    payload_type: ClassVar[type[QueuePayload]] = ChunkJob
    priority: ClassVar[int] = JobPriority.chunk
    concurrency_limit: ClassVar[int] = settings.graph_build_concurrency

    def __init__(self, clients: GraphClients) -> None:
        self.clients = clients

    async def handle(self, payload: ChunkJob) -> None:
        key = frozenset(payload.scopes)
        async with User.system(key) as session:
            chunk = await session.get(Chunk, payload.chunk_id)
        if chunk is None:
            logger.warning(
                "chunk {} not visible in scope {}, skipping",
                payload.chunk_id,
                ",".join(map(str, sorted(key))),
            )
            return
        if frozenset(chunk.scopes) != key:
            logger.warning("chunk {} does not belong to its queued scope, skipping", chunk.id)
            return
        touched = await extract_and_consolidate(chunk, self.clients)
        if not touched:
            return
        async with User.system(key) as session:
            await Watermark.bump_many(
                session,
                key,
                Watermark.Kind.entity_dirty,
                [str(entity_id) for entity_id in touched],
            )


async def enqueue_pending(
    limit: int | None = None,
    scopes: Scopes | None = None,
    source: str | None = None,
) -> int:
    """Enqueue a durable job for every pending chunk and return how many were queued."""
    key = frozenset(scopes or (settings.system_user_id,))
    chunks = await pending_chunks(key, limit, source)
    return await enqueue_chunks(chunks, key)


async def enqueue_document(document_id: UUID7, scopes: Scopes) -> int:
    """Enqueue only the pending chunks belonging to one newly ingested document."""
    key = frozenset(scopes)
    chunks = await pending_chunks(key, None, None, document_id)
    return await enqueue_chunks(chunks, key)


async def enqueue_chunks(chunks: Sequence[Chunk], scopes: Scopes) -> int:
    """Enqueue an explicit chunk set with stable per-chunk deduplication."""
    async with Queue(dsn=settings.asyncpg_dsn) as queue:
        queued = sum(
            [
                await queue.enqueue(
                    ChunkProjectionJob,
                    ChunkJob(chunk_id=chunk.id, scopes=scopes),
                    str(chunk.id),
                )
                for chunk in chunks
            ]
        )
    logger.info("enqueued {} pending chunks", queued)
    return queued


async def retry_failed_chunks(limit: int = 100) -> int:
    """Requeue retained chunk projection failures through PgQueuer."""
    async with Queue(dsn=settings.asyncpg_dsn) as queue:
        return await queue.requeue_failed(ChunkProjectionJob, limit)
