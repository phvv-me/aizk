from functools import partial
from typing import TYPE_CHECKING, cast

from loguru import logger
from pydantic import UUID5
from sqlmodel.sql.expression import Select

from ..config import DatabaseBackend, settings
from ..ontology import Ontology
from ..store import Artifact, Document, SessionItem
from ..store.identity import User
from ..types import Scopes
from ..usage import UsageAccountingJob
from .jobs.maintenance import ScheduledJob, ScopedScheduledJob, SystemScheduledJob
from .jobs.models import MaintenanceJob
from .jobs.projection import ChunkProjectionJob
from .portable import PortableJob, PortableWorker
from .queue import Queue

if TYPE_CHECKING:
    from ..runtime import Runtime


async def fan_out(job: ScopedScheduledJob) -> None:
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
        # `scope_sets` unions to a `CompoundSelect`, which sqlmodel's `exec` runs (returning
        # the same tuple rows a `Select` would) but does not cover in its overloads.
        rows = await db.exec(
            cast("Select[tuple[list[UUID5]]]", Document.scope_sets(SessionItem, Artifact))
        )
        keys = {frozenset(scopes) for (scopes,) in rows if scopes}
        return sorted(keys, key=lambda scopes: sorted(scopes))


def portable_worker(runtime: Runtime, batch_size: int | None = None) -> PortableWorker:
    """Assemble the CockroachDB worker with every queue and schedule callback."""
    batch = batch_size or settings.queue_batch_size
    scheduled = [job_type.assemble(runtime) for job_type in ScheduledJob.implementations()]
    queued_jobs: list[PortableJob] = [
        ChunkProjectionJob(runtime.graph),
        UsageAccountingJob(),
        runtime.artifacts.conversion,
        *(job for job in scheduled if isinstance(job, ScopedScheduledJob)),
    ]
    schedules = {
        job.cron_entrypoint: (
            job.expression,
            partial(fan_out, job)
            if isinstance(job, ScopedScheduledJob)
            else partial(cast(SystemScheduledJob, job).execute),
        )
        for job in scheduled
        if job.enabled
    }
    return PortableWorker(queued_jobs, schedules, batch)


async def run_worker_once(runtime: Runtime, batch_size: int | None = None) -> int:
    """Drain one CockroachDB batch and due schedule wave for serverless invocation."""
    if settings.database_backend is not DatabaseBackend.cockroachdb:
        raise RuntimeError("serverless queue draining requires CockroachDB")
    async with User.system() as session:
        await Ontology.refresh(session)
    worker = portable_worker(runtime, batch_size)
    await worker.install_schedules()
    return await worker.run_once()


async def run_worker(runtime: Runtime, batch_size: int | None = None) -> None:
    """Run the autonomous engine, draining on-write jobs and firing the scheduled passes."""
    batch = batch_size or settings.queue_batch_size
    async with User.system() as session:
        await Ontology.refresh(session)
    if settings.database_backend is DatabaseBackend.cockroachdb:
        await portable_worker(runtime, batch_size).run()
        return
    async with Queue(dsn=settings.asyncpg_dsn) as queue:
        pg = queue.worker()
        ChunkProjectionJob(runtime.graph).bind(pg)
        UsageAccountingJob().bind(pg)
        runtime.artifacts.conversion.bind(pg)
        for job_type in ScheduledJob.implementations():
            job_type.assemble(runtime).register(pg, fan_out)
        logger.info("autonomous worker listening on the queue and the scheduler")
        await pg.run(batch_size=batch, max_concurrent_tasks=batch * 4)
