import uuid
from collections.abc import AsyncIterator, Iterable
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
from ..store import Chunk, Watermark, acting_as
from ..store.context import session
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
async def queue_connection() -> AsyncIterator[asyncpg.Connection]:
    """Open a short asyncpg connection on the app DSN, closing it when the block exits."""
    connection = await asyncpg.connect(settings.asyncpg_dsn)
    try:
        yield connection
    finally:
        await connection.close()


@asynccontextmanager
async def queue_queries() -> AsyncIterator[Queries]:
    """Open a short connection on the app DSN and hand back its pgqueuer Queries, closing after.

    The one seam every enqueue path in this module shares, so a `Queries` is never hand-assembled
    from a raw connection more than once.
    """
    async with queue_connection() as connection:
        yield Queries(AsyncpgDriver(connection))


async def enqueue_deduped(
    queries: Queries, entrypoint: str, payload: JobPayload, dedupe_key: str
) -> bool:
    """Enqueue one job, deduplicated on its own key, returning whether it was newly queued.

    Every enqueue path shares this one try/except, `DuplicateJobError` the documented signal that
    a job with this dedupe_key is already waiting or in flight, harmless to swallow.

    queries: the open pgqueuer session this job enqueues through.
    entrypoint: the worker entrypoint name the job is dispatched to.
    payload: the job's own typed payload, encoded to bytes before enqueue.
    dedupe_key: the key pgqueuer deduplicates concurrent enqueues of the same work on.
    """
    try:
        await queries.enqueue(entrypoint, payload.encode(), dedupe_key=dedupe_key)
    except DuplicateJobError:
        return False
    return True


async def process_chunk(chunk_id: uuid.UUID, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Build one chunk's graph slice under its owner and return the entity ids it touched.

    The durable per-job wrapper around extract_and_consolidate, the same core build_graph's
    inline concurrent loop runs, so the queue worker and force_rebuild extract, resolve, and
    consolidate identically and share the one extraction_semaphore concurrency ceiling. Bumps a
    dirty watermark for every touched entity on top, the on-write signal a debounced profile
    rebuild reads, since that chaining is this queue path's own concern rather than the shared
    core's.

    chunk_id: chunk whose graph slice to build.
    user_id: identity that owns the written entities and facts.
    """
    async with acting_as(user_id):
        chunk = await session().get(Chunk, chunk_id)
    if chunk is None:
        logger.warning("chunk {} not visible to {}, skipping", chunk_id, user_id)
        return []
    touched = await extract_and_consolidate(chunk, user_id)
    if touched:
        async with acting_as(user_id):
            for entity_id in touched:
                await Watermark.bump(user_id, Watermark.Kind.entity_dirty, ref=str(entity_id))
    return list(touched)


async def process_profile(entity_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Rebuild one entity's profile under its owner and clear its dirty watermark.

    entity_id: entity whose profile to rebuild.
    user_id: identity that owns the profile.
    """
    await build_profile(entity_id, user_id=user_id)
    async with acting_as(user_id):
        await Watermark.set_value(
            user_id, Watermark.Kind.entity_dirty, counter=0, ref=str(entity_id)
        )


async def enqueue_pending(
    limit: int | None = None,
    user_id: uuid.UUID | None = None,
    source: str | None = None,
) -> int:
    """Enqueue a durable job for every pending chunk and return how many were queued.

    Each job is deduplicated on its chunk id, so enqueuing twice is harmless.

    limit: maximum number of chunks to enqueue, all of them when null.
    user_id: identity whose visibility scopes the chunks and that owns the written rows, the
        system user when null.
    source: when set, restrict to chunks of documents whose title matches this substring.
    """
    user_id = user_id or settings.system_user_id
    chunks = await pending_chunks(user_id, limit, source)
    async with queue_queries() as queries:
        queued = sum(
            [
                await enqueue_deduped(
                    queries,
                    EXTRACT_ENTRYPOINT,
                    ChunkJob(chunk_id=chunk.id, user_id=user_id),
                    str(chunk.id),
                )
                for chunk in chunks
            ]
        )
    logger.info("enqueued {} pending chunks", queued)
    return queued


async def enqueue_profiles(entity_ids: Iterable[uuid.UUID], user_id: uuid.UUID) -> None:
    """Enqueue a debounced profile-rebuild job for each touched entity, the on-write chain.

    Each job is deduplicated on its entity, so a burst of writes touching one entity collapses to a
    single rebuild while that rebuild is still queued or in flight.

    entity_ids: the entities a finished extraction touched.
    user_id: identity that owns the profiles.
    """
    entity_ids = list(entity_ids)
    if not entity_ids:
        return
    async with queue_queries() as queries:
        for entity_id in entity_ids:
            await enqueue_deduped(
                queries,
                PROFILE_ENTRYPOINT,
                ProfileJob(entity_id=entity_id, user_id=user_id),
                f"profile:{entity_id}",
            )


async def grant_queue_access(connection: asyncpg.Connection, role: str) -> None:
    """Grant the app role DML on the queue tables and USAGE on their serial sequences.

    connection: open connection on the owner DSN, since only the owner may GRANT.
    role: the restricted app role the worker and enqueue paths connect as.
    """
    for table in QUEUE_TABLES:
        await connection.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")
    for sequence in QUEUE_SEQUENCES:
        await connection.execute(f"GRANT USAGE, SELECT ON SEQUENCE {sequence} TO {role}")


async def install_queue_schema() -> None:
    """Install the pgqueuer tables and grant the app role access, run as the owner.

    Connects on the owner DSN, since the owner creates the tables but the app connects under the
    non-owning role. A re-run is tolerated, since a pre-existing schema only means the install
    already ran.
    """
    connection = await asyncpg.connect(settings.admin_asyncpg_dsn)
    try:
        try:
            await Queries(AsyncpgDriver(connection)).install()
        except DuplicateFunctionError, DuplicateObjectError, DuplicateTableError:
            logger.info("pgqueuer schema already installed")
        await grant_queue_access(connection, settings.app_role)
    finally:
        await connection.close()
    logger.info("pgqueuer schema installed and granted to {}", settings.app_role)
