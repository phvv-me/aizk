import uuid
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager

import asyncpg
from asyncpg.exceptions import DuplicateFunctionError, DuplicateObjectError, DuplicateTableError
from loguru import logger
from pgqueuer import Queries
from pgqueuer.db import AsyncpgDriver
from pgqueuer.errors import DuplicateJobError

from ..config import settings
from ..graph.build import extract_and_consolidate, pending_chunks
from ..graph.profiles import build_profile
from ..store import Chunk, Watermark
from ..store.ddl import Grant, GrantTarget, postgresql_sql
from ..store.identity import User
from ..types import Scopes
from .payloads import ChunkJob, JobPayload, ProfileJob

# the on-write entrypoint, one durable job per pending chunk, building its graph slice then
# chaining a debounced profile rebuild for every entity the slice touched
EXTRACT_ENTRYPOINT = "aizk_build_graph_chunk"

# the follow-up entrypoint a finished extraction chains, rebuilding one touched entity's profile,
# deduplicated on the entity so a burst of writes collapses to one rebuild while it is in flight
PROFILE_ENTRYPOINT = "aizk_build_profile"

# tables and serial sequences pgqueuer creates as the owner, which the restricted app role must be
# granted before it can enqueue from the build path and dequeue from the worker
QUEUE_TABLES = ("pgqueuer", "pgqueuer_log", "pgqueuer_statistics", "pgqueuer_schedules")
QUEUE_SEQUENCES = ("pgqueuer_id_seq", "pgqueuer_statistics_id_seq", "pgqueuer_schedules_id_seq")


@asynccontextmanager
async def queue_connection() -> AsyncGenerator[asyncpg.Connection]:
    """Open a short asyncpg connection on the app DSN, closing it when the block exits."""
    connection = await asyncpg.connect(settings.asyncpg_dsn)
    try:
        yield connection
    finally:
        await connection.close()


@asynccontextmanager
async def queue_queries() -> AsyncGenerator[Queries]:
    """Open a short connection on the app DSN and hand back its pgqueuer Queries, closing
    after."""
    async with queue_connection() as connection:
        yield Queries(AsyncpgDriver(connection))


async def enqueue_deduped(
    queries: Queries, entrypoint: str, payload: JobPayload, dedupe_key: str
) -> bool:
    """Enqueue one job, deduplicated on its own key, returning whether it was newly queued."""
    try:
        await queries.enqueue(entrypoint, payload.encode(), dedupe_key=dedupe_key)
    except DuplicateJobError:
        return False
    return True


async def process_chunk(chunk_id: uuid.UUID, scopes: Scopes) -> list[uuid.UUID]:
    """Build one chunk's graph slice in its exact scope and return touched entity ids."""
    key = frozenset(scopes)
    async with User.system(key) as session:
        chunk = await session.get(Chunk, chunk_id)
    if chunk is None:
        logger.warning(
            "chunk {} not visible in scope {}, skipping",
            chunk_id,
            ",".join(map(str, sorted(key))),
        )
        return []
    if frozenset(chunk.scopes) != key:
        logger.warning("chunk {} does not belong to its queued scope, skipping", chunk_id)
        return []
    touched = await extract_and_consolidate(chunk)
    if touched:
        async with User.system(key) as session:
            await Watermark.bump_many(
                session,
                key,
                Watermark.Kind.entity_dirty,
                [str(entity_id) for entity_id in touched],
            )
    return list(touched)


async def process_profile(entity_id: uuid.UUID, scopes: Scopes) -> None:
    """Rebuild one entity's profile in an exact scope and clear its dirty watermark."""
    key = frozenset(scopes)
    await build_profile(entity_id, scopes=key)
    async with User.system(key) as session:
        await Watermark.set_value(
            session, key, Watermark.Kind.entity_dirty, counter=0, ref=str(entity_id)
        )


async def enqueue_pending(
    limit: int | None = None,
    scopes: Scopes | None = None,
    source: str | None = None,
) -> int:
    """Enqueue a durable job for every pending chunk and return how many were queued."""
    key = frozenset(scopes or (settings.system_user_id,))
    chunks = await pending_chunks(key, limit, source)
    async with queue_queries() as queries:
        queued = sum(
            [
                await enqueue_deduped(
                    queries,
                    EXTRACT_ENTRYPOINT,
                    ChunkJob(chunk_id=chunk.id, scopes=key),
                    str(chunk.id),
                )
                for chunk in chunks
            ]
        )
    logger.info("enqueued {} pending chunks", queued)
    return queued


async def enqueue_profiles(entity_ids: Iterable[uuid.UUID], scopes: Scopes) -> None:
    """Enqueue a debounced profile-rebuild job for each touched entity, the on-write chain."""
    entity_ids = list(entity_ids)
    if not entity_ids:
        return
    key = frozenset(scopes)
    async with queue_queries() as queries:
        for entity_id in entity_ids:
            await enqueue_deduped(
                queries,
                PROFILE_ENTRYPOINT,
                ProfileJob(entity_id=entity_id, scopes=key),
                f"profile:{','.join(map(str, sorted(key)))}:{entity_id}",
            )


async def grant_queue_access(connection: asyncpg.Connection, role: str) -> None:
    """Grant the app role DML on the queue tables and USAGE on their serial sequences."""
    for table in QUEUE_TABLES:
        grant = Grant(
            GrantTarget.table,
            table,
            role,
            ("SELECT", "INSERT", "UPDATE", "DELETE"),
        )
        await connection.execute(postgresql_sql(grant))
    for sequence in QUEUE_SEQUENCES:
        grant = Grant(GrantTarget.sequence, sequence, role, ("USAGE", "SELECT"))
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
        await grant_queue_access(connection, settings.app_role)
    finally:
        await connection.close()
    logger.info("pgqueuer schema installed and granted to {}", settings.app_role)
