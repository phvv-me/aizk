from loguru import logger
from pgqueuer import PgQueuer, Queries
from pgqueuer.db import AsyncpgDriver
from pgqueuer.errors import DuplicateJobError
from pgqueuer.models import Job

from ..config import settings
from ..store import Principal, system_session
from .payloads import ChunkJob, ProfileJob, TaskJob
from .queue import (
    EXTRACT_ENTRYPOINT,
    PROFILE_ENTRYPOINT,
    enqueue_profiles,
    process_chunk,
    process_profile,
    queue_connection,
)
from .tasks import ScheduledTask


async def fan_out(task: ScheduledTask) -> None:
    """Read the principal roster as the system principal and enqueue one task job per principal.

    The load-bearing no-leak boundary. A scheduled pass fires outside any principal scope, so it
    reads every principal once as the system identity, then enqueues a separate job carrying each
    principal id whose body runs inside acting_as that principal. No pass ever reads the roster and
    writes another principal's rows in the same transaction. Each job is deduplicated on the task
    and principal so a pass that fires while the last one is still draining never piles up.

    task: the scheduled task being fanned out.
    """
    async with system_session() as session:
        principals = await Principal.list_all(session)
    queued = 0
    async with queue_connection() as connection:
        queries = Queries(AsyncpgDriver(connection))
        for principal in principals:
            job = TaskJob(principal_id=principal.id)
            try:
                await queries.enqueue(
                    task.queue_entrypoint, job.encode(), dedupe_key=f"{task.name}:{principal.id}"
                )
            except DuplicateJobError:
                continue  # this principal's pass is still queued or draining from the last fire
            queued += 1
    logger.info("fan-out {} enqueued {} principal jobs", task.name, queued)


async def run_worker(batch_size: int = 10) -> None:
    """Run the autonomous engine, draining on-write jobs and firing the scheduled passes.

    Registers the extraction entrypoint, which builds a chunk's graph slice then chains a debounced
    profile rebuild for every entity it touched, the profile entrypoint that runs those rebuilds,
    and one queue-and-cron pair per registered `ScheduledTask`. `pg.run` then runs both the queue
    manager and the scheduler at once until interrupted, so a single `aizk worker` is the whole
    self-maintaining engine.

    batch_size: maximum number of jobs the manager dequeues per round.
    """
    async with queue_connection() as connection:
        pg = PgQueuer.from_asyncpg_connection(connection)

        @pg.entrypoint(EXTRACT_ENTRYPOINT)
        async def build_chunk(job: Job) -> None:
            """Build a chunk's graph slice then chain a profile rebuild for what it touched.

            job: dequeued job whose payload names the chunk and owning principal.
            """
            assert job.payload is not None
            chunk_job = ChunkJob.decode(job.payload)
            touched = await process_chunk(chunk_job.chunk_id, chunk_job.principal_id)
            if settings.profile_on_write:
                await enqueue_profiles(touched, chunk_job.principal_id)

        @pg.entrypoint(PROFILE_ENTRYPOINT)
        async def build_profile_chunk(job: Job) -> None:
            """Rebuild one touched entity's profile under its owner.

            job: dequeued job whose payload names the entity and owning principal.
            """
            assert job.payload is not None
            profile_job = ProfileJob.decode(job.payload)
            await process_profile(profile_job.entity_id, profile_job.principal_id)

        for task_cls in ScheduledTask.implementations():
            task_cls().register(pg, fan_out)

        logger.info("autonomous worker listening on the queue and the scheduler")
        await pg.run(batch_size=batch_size)
