from collections.abc import Sequence

import asyncpg
from asyncpg.exceptions import DuplicateFunctionError, DuplicateObjectError, DuplicateTableError
from loguru import logger
from pgqueuer import Queries
from pgqueuer.db import AsyncpgDriver
from pydantic import UUID7
from sqlalchemy import Column as SAColumn
from sqlalchemy import Index, MetaData, String, Table, and_
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.schema import CreateIndex, DropIndex

from ..common.queue import Queue, QueueSchema
from ..config import settings
from ..graph.build import pending_chunks
from ..store import Chunk
from ..store.ddl import Grant, GrantTarget, postgresql_sql
from ..types import Scopes
from .enum import QueueStatus
from .jobs.models import ChunkJob
from .jobs.projection import ChunkProjectionJob


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
    job = ChunkProjectionJob()
    async with Queue(dsn=settings.asyncpg_dsn) as queue:
        queued = sum(
            [
                await job.enqueue(
                    queue,
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
        return await queue.requeue_failed(ChunkProjectionJob(), limit)


async def grant_queue_access(
    connection: asyncpg.Connection,
    role: str,
    schema: QueueSchema,
) -> None:
    """Grant the app role only the objects PgQueuer reports installing."""
    grants = (
        *(
            Grant(
                GrantTarget.table,
                table,
                role,
                ("SELECT", "INSERT", "UPDATE", "DELETE"),
            )
            for table in schema.tables
        ),
        *(
            Grant(GrantTarget.sequence, sequence, role, ("USAGE", "SELECT"))
            for sequence in schema.sequences
        ),
    )
    for grant in grants:
        await connection.execute(postgresql_sql(grant))


async def install_queue_schema() -> None:
    """Install the pgqueuer tables and grant the app role access, run as the owner."""
    connection = await asyncpg.connect(settings.admin_asyncpg_dsn)
    try:
        queries = Queries(AsyncpgDriver(connection))
        try:
            await queries.install()
        except DuplicateFunctionError, DuplicateObjectError, DuplicateTableError:
            await queries.upgrade()
            logger.info("pgqueuer schema upgraded")
        schema = QueueSchema.from_settings(queries.qbe.settings)
        queue = Table(
            schema.queue,
            MetaData(),
            SAColumn("dedupe_key", String),
            SAColumn(
                "status",
                ENUM(QueueStatus, name=schema.status_type, create_type=False),
            ),
        )
        dedupe = Index(
            f"{schema.queue}_unique_dedupe_key",
            queue.c.dedupe_key,
            unique=True,
            postgresql_where=and_(
                queue.c.dedupe_key.is_not(None),
                queue.c.status.in_((QueueStatus.queued, QueueStatus.picked, QueueStatus.failed)),
            ),
        )
        await connection.execute(postgresql_sql(DropIndex(dedupe, if_exists=True)))
        await connection.execute(postgresql_sql(CreateIndex(dedupe)))
        await grant_queue_access(connection, settings.app_role, schema)
    finally:
        await connection.close()
    logger.info("pgqueuer schema installed and granted to {}", settings.app_role)
