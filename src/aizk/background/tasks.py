import abc
from collections.abc import Awaitable, Callable
from functools import partial

from loguru import logger
from patos import FrozenModel, Registry
from pgqueuer import PgQueuer
from pgqueuer.models import Job, Schedule
from sqlalchemy import func
from sqlmodel import select

from ..backup import scheduled_backup
from ..config import settings
from ..eval import EvalReport, run_eval
from ..graph.communities import build_communities
from ..graph.decay import decay
from ..graph.insight import derive_insights
from ..graph.profiles import refresh_profiles
from ..graph.raptor import build_raptor
from ..graph.repair import dedup_entities
from ..graph.session_tier import promote_sessions
from ..store import FactClaim, Watermark
from ..store.engine import Session
from ..store.identity import User
from ..types import Scopes
from .payloads import TaskJob

FanOut = Callable[["ScheduledTask"], Awaitable[None]]


class ScheduledTask(Registry, FrozenModel, abc.ABC):
    """One background maintenance pass the scheduler fans out across exact scope sets."""

    @property
    def queue_entrypoint(self) -> str:
        """Name of the queue entrypoint that runs this task for one scope key."""
        return f"aizk_task_{self.name}"

    @property
    def cron_entrypoint(self) -> str:
        """Name of the cron entrypoint that fans this task out across the users."""
        return f"aizk_cron_{self.name}"

    @property
    def expression(self) -> str:
        """Crontab expression this task's cron fan-out fires on."""
        return getattr(settings, f"{self.name}_cron")

    @property
    def enabled(self) -> bool:
        """Whether the scheduler registers this task's cron at all."""
        return getattr(settings, f"{self.name}_enabled")

    @abc.abstractmethod
    async def execute(self, scopes: Scopes) -> None:
        """Run this task body for one exact scope set."""

    async def run(self, scopes: Scopes) -> None:
        """Execute this task for one exact scope set."""
        await self.execute(frozenset(scopes))

    async def run_job(self, job: Job) -> None:
        """Run this task body for the exact scope key in its dequeued payload."""
        assert job.payload is not None
        await self.run(TaskJob.decode(job.payload).scopes)

    async def fire_cron(self, fan_out: FanOut, schedule: Schedule) -> None:
        """Fan this task out across the stored scope roster on its cron cadence."""
        await fan_out(self)

    def register(self, pg: PgQueuer, fan_out: FanOut) -> None:
        """Register this task's queue entrypoint always, and its cron fan-out only when
        enabled."""
        pg.entrypoint(self.queue_entrypoint)(self.run_job)
        if not self.enabled:
            return
        pg.schedule(self.cron_entrypoint, self.expression)(partial(self.fire_cron, fan_out))


async def recorded_fact_count(session: Session, scopes: Scopes) -> int:
    """Count of every fact claim ever recorded, the monotonic growth signal the gated passes
    read."""
    return (
        await session.exec(
            select(func.count())
            .select_from(FactClaim)
            .where(FactClaim.scopes == sorted(scopes))
            .execution_options(**{settings.skip_live_gate: True})
        )
    ).one()


async def run_if_grown(
    scopes: Scopes,
    kind: Watermark.Kind,
    threshold: int,
    build: Callable[[], Awaitable[int]],
    label: str,
) -> None:
    """Run a growth-gated rebuild only once the graph grew by threshold facts since its
    watermark."""
    async with User.system(scopes) as session:
        current = await recorded_fact_count(session, scopes)
        last = await Watermark.read(session, scopes, kind)
    if current - last < threshold:
        logger.info("{} pass skipped for {}, {} new facts", label, scopes, current - last)
        return
    await build()
    async with User.system(scopes) as session:
        await Watermark.set_value(session, scopes, kind, counter=current)


def per_config_best(per_config: dict[str, float]) -> str | None:
    """The swept config label with the highest score, null when nothing was scored."""
    return max(per_config, key=lambda label: per_config[label]) if per_config else None


async def store_scorecard(
    session: Session, scopes: Scopes, report: EvalReport, best: str | None
) -> None:
    """Persist the weekly self-eval scorecard as the scorecard watermark's payload."""
    await Watermark.set_value(
        session,
        scopes,
        Watermark.Kind.scorecard,
        counter=report.n,
        payload={
            "hit_at_k": report.hit_at_k,
            "ndcg_at_k": report.ndcg_at_k,
            "mrr": report.mrr,
            "per_config": report.per_config,
            "best": best,
            "significant_best": report.significant_best,
        },
    )


class DecayTask(ScheduledTask):
    """Archive stale facts past their half-life, the daily decay pass."""

    name = "decay"

    async def execute(self, scopes: Scopes) -> None:
        await decay(scopes=scopes, half_life_days=settings.decay_half_life_days)


class DedupTask(ScheduledTask):
    """Merge duplicate entities, the nightly dedup pass."""

    name = "dedup"

    async def execute(self, scopes: Scopes) -> None:
        await dedup_entities(scopes=scopes)


class CommunitiesTask(ScheduledTask):
    """Rebuild communities, clusters of related entities detected from the fact graph, once
    it grew by `communities_every_n_facts` facts, the weekly gate."""

    name = "communities"

    async def execute(self, scopes: Scopes) -> None:
        await run_if_grown(
            scopes,
            Watermark.Kind.fact_count,
            settings.communities_every_n_facts,
            partial(build_communities, scopes=scopes),
            "community",
        )


class RaptorTask(ScheduledTask):
    """Rebuild the RAPTOR tree, the hierarchical summary tree recall reads for thematic
    queries, once the graph grew by `raptor_every_n_facts` facts, the RAPTOR gate."""

    name = "raptor"

    async def execute(self, scopes: Scopes) -> None:
        await run_if_grown(
            scopes,
            Watermark.Kind.raptor_fact_count,
            settings.raptor_every_n_facts,
            partial(build_raptor, scopes=scopes),
            "raptor",
        )


class ProfileRefreshTask(ScheduledTask):
    """Rebuild every profile, the weekly full refresh pass."""

    name = "profile_refresh"

    async def execute(self, scopes: Scopes) -> None:
        await refresh_profiles(scopes=scopes)


class SelfImproveTask(ScheduledTask):
    """Score recall and store the weekly retrieval scorecard."""

    name = "self_improve"

    async def execute(self, scopes: Scopes) -> None:
        report = await run_eval(None, user=User.system(scopes))
        best = per_config_best(report.per_config)
        async with User.system(scopes) as session:
            await store_scorecard(session, scopes, report, best)
        logger.info(
            "self-improve scored {} items for {}, best {}",
            report.n,
            scopes,
            best,
        )


class SessionPromoteTask(ScheduledTask):
    """Promote aged working items into the graph, the quarter-hour promotion pass."""

    name = "session_promote"

    async def execute(self, scopes: Scopes) -> None:
        await promote_sessions(scopes=scopes)


class InsightTask(ScheduledTask):
    """Derive reflective observations over the graph, the weekly insight pass."""

    name = "insight"

    async def execute(self, scopes: Scopes) -> None:
        await derive_insights(scopes=scopes)


class BackupTask(ScheduledTask):
    """Dump the whole database on a cron, the integrated auto-backup pass the worker runs."""

    name = "backup"

    async def execute(self, scopes: Scopes) -> None:
        raise NotImplementedError("backup is a system pass, never fanned out per scope")

    async def fire_cron(self, fan_out: FanOut, schedule: Schedule) -> None:
        """Dump and prune directly on the cron tick, no per-scope fan-out."""
        report = await scheduled_backup()
        logger.info("scheduled backup wrote {} bytes to {}", report.bytes, report.path)

    def register(self, pg: PgQueuer, fan_out: FanOut) -> None:
        """Register only the backup cron, and only when enabled, no per-scope entrypoint."""
        if self.enabled:
            pg.schedule(self.cron_entrypoint, self.expression)(partial(self.fire_cron, fan_out))
