from loguru import logger
from pgqueuer import PgQueuer
from pgqueuer.models import Job
from sqlmodel import select

from ..config import settings
from ..extract import ontology
from ..store import Document, SessionItem, as_system
from ..store.engine import bypass_rls
from ..types import Scopes
from .payloads import ChunkJob, ProfileJob, TaskJob
from .queue import (
    EXTRACT_ENTRYPOINT,
    PROFILE_ENTRYPOINT,
    enqueue_deduped,
    enqueue_profiles,
    process_chunk,
    process_profile,
    queue_connection,
    queue_queries,
)
from .tasks import ScheduledTask


async def fan_out(task: ScheduledTask) -> None:
    """Read the distinct scope roster past row security and enqueue one task per scope."""
    scopes = await scope_roster()
    async with queue_queries() as queries:
        queued = sum(
            [
                await enqueue_deduped(
                    queries,
                    task.queue_entrypoint,
                    TaskJob(scopes=key),
                    f"{task.name}:{','.join(map(str, sorted(key)))}",
                )
                for key in scopes
            ]
        )
    logger.info("fan-out {} enqueued {} scope jobs", task.name, queued)


async def scope_roster() -> list[Scopes]:
    """Every exact scope set with stored memory, read under the database administrator role."""
    async with bypass_rls() as db:
        rows = (await db.exec(select(Document.scopes).union(select(SessionItem.scopes)))).scalars()
        return sorted({frozenset(row) for row in rows if row}, key=lambda scopes: sorted(scopes))


async def handle_chunk_job(job: Job) -> None:
    """Build one dequeued chunk's graph slice, chaining a profile enqueue for what it
    touched."""
    assert job.payload is not None
    chunk_job = ChunkJob.decode(job.payload)
    touched = await process_chunk(chunk_job.chunk_id, chunk_job.scopes)
    if settings.profile_on_write:
        await enqueue_profiles(touched, chunk_job.scopes)


async def handle_profile_job(job: Job) -> None:
    """Rebuild one dequeued job's touched entity profile under its owning user."""
    assert job.payload is not None
    profile_job = ProfileJob.decode(job.payload)
    await process_profile(profile_job.entity_id, profile_job.scopes)


async def run_worker(batch_size: int = 10) -> None:
    """Run the autonomous engine, draining on-write jobs and firing the scheduled passes."""
    async with as_system() as session:
        await ontology.refresh(session)
    async with queue_connection() as connection:
        pg = PgQueuer.from_asyncpg_connection(connection)
        pg.entrypoint(EXTRACT_ENTRYPOINT, concurrency_limit=settings.graph_build_concurrency)(
            handle_chunk_job
        )
        pg.entrypoint(PROFILE_ENTRYPOINT)(handle_profile_job)
        for task_cls in ScheduledTask.implementations():
            task_cls().register(pg, fan_out)
        logger.info("autonomous worker listening on the queue and the scheduler")
        await pg.run(batch_size=batch_size, max_concurrent_tasks=batch_size * 4)
