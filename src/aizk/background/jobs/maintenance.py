import abc
from collections.abc import Awaitable, Callable
from functools import partial
from typing import ClassVar

from loguru import logger
from patos import FrozenModel, Registry
from pgqueuer import PgQueuer
from pgqueuer.models import Schedule
from sqlalchemy import func
from sqlmodel import select

from ...backup import scheduled_backup
from ...common.queue import QueueJob
from ...config import settings
from ...graph.communities import build_communities
from ...graph.decay import decay
from ...graph.insight import derive_insights
from ...graph.profiles import refresh_dirty_profiles, refresh_profiles
from ...graph.raptor import build_raptor
from ...graph.repair import dedup_entities
from ...graph.session_tier import promote_sessions
from ...store import Fact, Watermark
from ...store.engine import Session
from ...store.identity import User
from ...types import Scopes
from ..enum import JobPriority
from .models import MaintenanceJob

FanOut = Callable[["ScheduledJob"], Awaitable[None]]


class ScheduledJob(Registry, FrozenModel, QueueJob[MaintenanceJob], abc.ABC):
    """One maintenance job fanned out across exact scope sets on a schedule."""

    payload_type: ClassVar[type[MaintenanceJob]] = MaintenanceJob
    priority: ClassVar[int] = JobPriority.maintenance
    concurrency_limit: ClassVar[int] = 1
    cron_entrypoint: ClassVar[str]

    def __init_subclass__(cls, **kwargs: bool) -> None:
        """Derive stable queue names from each registered job name."""
        super().__init_subclass__(**kwargs)
        cls.entrypoint = f"aizk_task_{cls.name}"
        cls.cron_entrypoint = f"aizk_cron_{cls.name}"

    @property
    def expression(self) -> str:
        return getattr(settings, f"{self.name}_cron")

    @property
    def enabled(self) -> bool:
        return getattr(settings, f"{self.name}_enabled")

    @abc.abstractmethod
    async def execute(self, scopes: Scopes) -> None:
        """Run this maintenance body for one exact scope set."""

    async def handle(self, payload: MaintenanceJob) -> None:
        await self.execute(frozenset(payload.scopes))

    async def fire_cron(self, fan_out: FanOut, schedule: Schedule) -> None:
        await fan_out(self)

    def register(self, worker: PgQueuer, fan_out: FanOut) -> None:
        self.bind(worker)
        if self.enabled:
            worker.schedule(self.cron_entrypoint, self.expression)(
                partial(self.fire_cron, fan_out)
            )


async def recorded_fact_count(session: Session, scopes: Scopes) -> int:
    """Count every recorded fact claim for a monotonic growth signal."""
    return (
        await session.exec(
            select(func.count())
            .select_from(Fact.Claim)
            .where(Fact.Claim.scopes == sorted(scopes))
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
    """Run a graph projection after its fact-growth threshold is reached."""
    async with User.system(scopes) as session:
        current = await recorded_fact_count(session, scopes)
        last = await Watermark.read(session, scopes, kind)
    if current - last < threshold:
        logger.info("{} pass skipped for {}, {} new facts", label, scopes, current - last)
        return
    await build()
    async with User.system(scopes) as session:
        await Watermark.set_value(session, scopes, kind, counter=current)


class DecayJob(ScheduledJob):
    """Archive stale facts past their half-life each day."""

    name = "decay"

    async def execute(self, scopes: Scopes) -> None:
        await decay(scopes=scopes, half_life_days=settings.decay_half_life_days)


class DedupJob(ScheduledJob):
    """Merge duplicate entities each night."""

    name = "dedup"

    async def execute(self, scopes: Scopes) -> None:
        await dedup_entities(scopes=scopes)


class CommunitiesJob(ScheduledJob):
    """Rebuild communities after enough new facts arrive."""

    name = "communities"

    async def execute(self, scopes: Scopes) -> None:
        await run_if_grown(
            scopes,
            Watermark.Kind.fact_count,
            settings.communities_every_n_facts,
            partial(build_communities, scopes=scopes),
            "community",
        )


class RaptorJob(ScheduledJob):
    """Rebuild the RAPTOR tree after enough new facts arrive."""

    name = "raptor"

    async def execute(self, scopes: Scopes) -> None:
        await run_if_grown(
            scopes,
            Watermark.Kind.raptor_fact_count,
            settings.raptor_every_n_facts,
            partial(build_raptor, scopes=scopes),
            "raptor",
        )


class ProfileProjectionJob(ScheduledJob):
    """Consume one bounded dirty-profile batch every minute."""

    name = "profile_projection"

    async def execute(self, scopes: Scopes) -> None:
        await refresh_dirty_profiles(scopes=scopes)


class ProfileRefreshJob(ScheduledJob):
    """Rebuild every profile on the weekly cadence."""

    name = "profile_refresh"

    async def execute(self, scopes: Scopes) -> None:
        await refresh_profiles(scopes=scopes)


class SessionPromoteJob(ScheduledJob):
    """Promote aged working items into the graph."""

    name = "session_promote"

    async def execute(self, scopes: Scopes) -> None:
        await promote_sessions(scopes=scopes)


class InsightJob(ScheduledJob):
    """Derive reflective observations over the graph each week."""

    name = "insight"

    async def execute(self, scopes: Scopes) -> None:
        await derive_insights(scopes=scopes)


class BackupJob(ScheduledJob):
    """Dump and prune the database on its configured cadence."""

    name = "backup"

    async def execute(self, scopes: Scopes) -> None:
        raise NotImplementedError("backup is a system pass, never fanned out per scope")

    async def fire_cron(self, fan_out: FanOut, schedule: Schedule) -> None:
        report = await scheduled_backup()
        logger.info("scheduled backup wrote {} bytes to {}", report.bytes, report.path)

    def register(self, worker: PgQueuer, fan_out: FanOut) -> None:
        if self.enabled:
            worker.schedule(self.cron_entrypoint, self.expression)(
                partial(self.fire_cron, fan_out)
            )
