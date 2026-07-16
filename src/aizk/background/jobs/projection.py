from typing import ClassVar

from loguru import logger

from ...common.queue import QueueJob
from ...config import settings
from ...graph.build import extract_and_consolidate
from ...store import Chunk, Watermark
from ...store.identity import User
from ..enum import JobPriority
from .models import ChunkJob


class ChunkProjectionJob(QueueJob[ChunkJob]):
    """Build one chunk graph projection and mark its touched profiles dirty."""

    entrypoint: ClassVar[str] = "aizk_build_graph_chunk"
    payload_type: ClassVar[type[ChunkJob]] = ChunkJob
    priority: ClassVar[int] = JobPriority.chunk
    concurrency_limit: ClassVar[int] = settings.graph_build_concurrency

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
        touched = await extract_and_consolidate(chunk)
        if not touched:
            return
        async with User.system(key) as session:
            await Watermark.bump_many(
                session,
                key,
                Watermark.Kind.entity_dirty,
                [str(entity_id) for entity_id in touched],
            )
