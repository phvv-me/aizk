import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager

import asyncpg
from asyncpg.exceptions import DuplicateObjectError, DuplicateTableError
from loguru import logger
from pgqueuer import Queries
from pgqueuer.db import AsyncpgDriver
from pgqueuer.errors import DuplicateJobError

from ..config import settings
from ..extract.llm import extract_triples, resolve_timestamps
from ..graph.build import GraphWriter, pending_chunks
from ..graph.profiles import build_profile
from ..store import Chunk, Watermark, acting_as
from .payloads import ChunkJob, ProfileJob

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


async def process_chunk(chunk_id: uuid.UUID, principal_id: uuid.UUID) -> list[uuid.UUID]:
    """Build one chunk's graph slice under its owner and return the entity ids it touched.

    Extracts and dates outside any write, then resolves entities and consolidates facts inside one
    owner-scoped transaction, bumping a dirty watermark for every touched entity.

    chunk_id: chunk whose graph slice to build.
    principal_id: identity that owns the written entities and facts.
    """
    async with acting_as(principal_id) as session:
        chunk = await session.get(Chunk, chunk_id)
    if chunk is None:
        logger.warning("chunk {} not visible to {}, skipping", chunk_id, principal_id)
        return []
    extraction = await extract_triples(chunk.text)
    dated_facts = await resolve_timestamps(chunk.text, extraction.facts)
    touched: set[uuid.UUID] = set()
    async with acting_as(principal_id) as session:
        writer = GraphWriter(session, principal_id, chunk.scope)
        for entity in extraction.entities:
            resolved = await writer.resolve(entity.name, entity.type)
            if resolved is not None:
                touched.add(resolved)
        for fact in dated_facts:
            await writer.consolidate(fact, chunk.id)
        for entity_id in touched:
            await Watermark.bump(
                session, principal_id, Watermark.Kind.entity_dirty, ref=str(entity_id)
            )
    logger.info("graph slice from chunk {} done", chunk_id)
    return list(touched)


async def process_profile(entity_id: uuid.UUID, principal_id: uuid.UUID) -> None:
    """Rebuild one entity's profile under its owner and clear its dirty watermark.

    entity_id: entity whose profile to rebuild.
    principal_id: identity that owns the profile.
    """
    await build_profile(entity_id, principal_id=principal_id)
    async with acting_as(principal_id) as session:
        await Watermark.set_value(
            session, principal_id, Watermark.Kind.entity_dirty, counter=0, ref=str(entity_id)
        )


async def enqueue_pending(
    limit: int | None = None,
    principal_id: uuid.UUID | None = None,
    source: str | None = None,
) -> int:
    """Enqueue a durable job for every pending chunk and return how many were queued.

    Each job is deduplicated on its chunk id, so enqueuing twice is harmless.

    limit: maximum number of chunks to enqueue, all of them when null.
    principal_id: identity whose visibility scopes the chunks and that owns the written rows, the
        system principal when null.
    source: when set, restrict to chunks of documents whose title matches this substring.
    """
    principal_id = principal_id or settings.system_principal_id
    chunks = await pending_chunks(principal_id, limit, source)
    queued = 0
    async with queue_connection() as connection:
        queries = Queries(AsyncpgDriver(connection))
        for chunk in chunks:
            job = ChunkJob(chunk_id=chunk.id, principal_id=principal_id)
            try:
                await queries.enqueue(EXTRACT_ENTRYPOINT, job.encode(), dedupe_key=str(chunk.id))
            except DuplicateJobError:
                continue  # already waiting or in flight, the documented harmless re-enqueue
            queued += 1
    logger.info("enqueued {} pending chunks", queued)
    return queued


async def enqueue_profiles(entity_ids: Iterable[uuid.UUID], principal_id: uuid.UUID) -> None:
    """Enqueue a debounced profile-rebuild job for each touched entity, the on-write chain.

    Each job is deduplicated on its entity, so a burst of writes touching one entity collapses to a
    single rebuild while that rebuild is still queued or in flight.

    entity_ids: the entities a finished extraction touched.
    principal_id: identity that owns the profiles.
    """
    entity_ids = list(entity_ids)
    if not entity_ids:
        return
    async with queue_connection() as connection:
        queries = Queries(AsyncpgDriver(connection))
        for entity_id in entity_ids:
            job = ProfileJob(entity_id=entity_id, principal_id=principal_id)
            try:
                await queries.enqueue(
                    PROFILE_ENTRYPOINT, job.encode(), dedupe_key=f"profile:{entity_id}"
                )
            except DuplicateJobError:
                continue  # a rebuild for this entity is already queued or in flight


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
        except (DuplicateObjectError, DuplicateTableError):
            logger.info("pgqueuer schema already installed")
        role = settings.app_role
        for table in QUEUE_TABLES:
            await connection.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")
        for sequence in QUEUE_SEQUENCES:
            await connection.execute(f"GRANT USAGE, SELECT ON SEQUENCE {sequence} TO {role}")
    finally:
        await connection.close()
    logger.info("pgqueuer schema installed and granted to {}", settings.app_role)
