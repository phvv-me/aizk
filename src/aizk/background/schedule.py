from loguru import logger
from pgqueuer import PgQueuer
from pgqueuer.models import Job

from ..config import settings
from ..extract import ontology
from ..store import User, system_session
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
    """Read the user roster as the system user and enqueue one task job per user.

    The load-bearing no-leak boundary. A scheduled pass fires outside any user scope, so it
    reads every user once as the system identity, then enqueues a separate job carrying each
    user id whose body runs inside acting_as that user. No pass ever reads the roster and
    writes another user's rows in the same transaction. Each job is deduplicated on the task
    and user so a pass that fires while the last one is still draining never piles up.

    task: the scheduled task being fanned out.
    """
    async with system_session():
        users = await User.list_all()
    async with queue_queries() as queries:
        queued = sum(
            [
                await enqueue_deduped(
                    queries,
                    task.queue_entrypoint,
                    TaskJob(user_id=user.id),
                    f"{task.name}:{user.id}",
                )
                for user in users
            ]
        )
    logger.info("fan-out {} enqueued {} user jobs", task.name, queued)


async def handle_chunk_job(job: Job) -> None:
    """Build one dequeued chunk's graph slice, chaining a profile enqueue for what it touched.

    job: dequeued job whose payload names the chunk and owning user.
    """
    assert job.payload is not None
    chunk_job = ChunkJob.decode(job.payload)
    touched = await process_chunk(chunk_job.chunk_id, chunk_job.user_id)
    if settings.profile_on_write:
        await enqueue_profiles(touched, chunk_job.user_id)


async def handle_profile_job(job: Job) -> None:
    """Rebuild one dequeued job's touched entity profile under its owning user.

    job: dequeued job whose payload names the entity and owning user.
    """
    assert job.payload is not None
    profile_job = ProfileJob.decode(job.payload)
    await process_profile(profile_job.entity_id, profile_job.user_id)


async def run_worker(batch_size: int = 10) -> None:
    """Run the autonomous engine, draining on-write jobs and firing the scheduled passes.

    Registers the extraction entrypoint, which builds a chunk's graph slice then chains a debounced
    profile rebuild for every entity it touched, the profile entrypoint that runs those rebuilds,
    and one queue-and-cron pair per registered `ScheduledTask`. `pg.run` then runs both the queue
    manager and the scheduler at once until interrupted, so a single `aizk worker` is the whole
    self-maintaining engine.

    The extraction entrypoint carries its own `concurrency_limit=settings.graph_build_concurrency`
    so it always has that many chunks in flight regardless of how many cheap profile-rebuild or
    scheduled-task jobs share the same dequeue round. `batch_size` must still comfortably exceed
    that width for pgqueuer to ever dequeue enough extraction jobs to fill it, the queue-side half
    of the fix, `settings.queue_batch_size` by default. `max_concurrent_tasks` is set well above
    `batch_size` since pgqueuer requires at least twice the batch size and this worker otherwise
    has no reason to cap it any tighter, each entrypoint's own concurrency_limit already the real
    throttle.

    The ontology cache refreshes here before the first job is dequeued, since the extraction
    gate reads it and a worker is not guaranteed to run in a process that already bootstrapped
    (the standalone `aizk worker` CLI, or a server started with `auto_setup` off, both crashed
    every chunk job with `OntologyNotReadyError` otherwise).

    batch_size: maximum number of jobs the manager dequeues per round.
    """
    async with system_session():
        await ontology.refresh()
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
