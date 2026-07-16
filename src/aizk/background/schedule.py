from loguru import logger
from sqlmodel import select

from ..common.queue import Queue
from ..config import settings
from ..ontology import Ontology
from ..store import Document, SessionItem
from ..store.identity import User
from ..types import Scopes
from .jobs.maintenance import ScheduledJob
from .jobs.models import MaintenanceJob
from .jobs.projection import ChunkProjectionJob


async def fan_out(job: ScheduledJob) -> None:
    """Read the distinct scope roster past row security and enqueue one task per scope."""
    scopes = await scope_roster()
    async with Queue(dsn=settings.asyncpg_dsn) as queue:
        queued = sum(
            [
                await job.enqueue(
                    queue,
                    MaintenanceJob(scopes=key),
                    f"{job.name}:{','.join(map(str, sorted(key)))}",
                )
                for key in scopes
            ]
        )
    logger.info("fan-out {} enqueued {} scope jobs", job.name, queued)


async def scope_roster() -> list[Scopes]:
    """Every exact scope set with stored memory, read under the database administrator role."""
    async with User.system().owner as db:
        rows = await db.exec(select(Document.scopes).union(select(SessionItem.scopes)))
        keys = {frozenset(scopes) for (scopes,) in rows if scopes}
        return sorted(keys, key=lambda scopes: sorted(scopes))


async def run_worker(batch_size: int = settings.queue_batch_size) -> None:
    """Run the autonomous engine, draining on-write jobs and firing the scheduled passes."""
    async with User.system() as session:
        await Ontology.refresh(session)
    async with Queue(dsn=settings.asyncpg_dsn) as queue:
        pg = queue.worker()
        ChunkProjectionJob().bind(pg)
        for job_type in ScheduledJob.implementations():
            job_type().register(pg, fan_out)
        logger.info("autonomous worker listening on the queue and the scheduler")
        await pg.run(batch_size=batch_size, max_concurrent_tasks=batch_size * 4)
