import abc
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING, ClassVar, Self, cast

import inflection
from loguru import logger
from patos import FrozenModel, Registry
from pgqueuer import PgQueuer
from pgqueuer.models import Schedule
from pydantic import ConfigDict
from sqlmodel import select

from ...artifacts.configured import ArtifactServices
from ...backup import scheduled_backup
from ...config import settings
from ...graph.communities import build_communities
from ...graph.decay import decay
from ...graph.insight import derive_insights
from ...graph.profiles import refresh_dirty_profiles, refresh_profiles
from ...graph.raptor import build_raptor
from ...graph.repair import dedup_entities
from ...graph.session_tier import promote_sessions
from ...serving.embed import Embedder
from ...serving.extract import LLM
from ...store import Fact, Watermark
from ...store.engine import Session
from ...store.identity import User
from ...types import Scopes
from ..enum import JobPriority
from ..queue import QueueJob, QueuePayload
from .models import MaintenanceJob

if TYPE_CHECKING:
    from ...runtime import Runtime

FanOut = Callable[["ScopedScheduledJob"], Awaitable[None]]


class ScheduledJob(Registry, FrozenModel, abc.ABC):
    """One automatically named maintenance pass on a configured schedule."""

    cron_entrypoint: ClassVar[str]

    def __init_subclass__(cls, **kwargs: bool) -> None:
        """Derive the job and cron names from the concrete class name."""
        super().__init_subclass__(**kwargs)
        cls.name = inflection.underscore(cls.__name__).removesuffix("_job")
        cls.cron_entrypoint = f"aizk_cron_{cls.name}"

    @classmethod
    def assemble(cls, runtime: Runtime) -> Self:
        """Build this job for the worker, taking only the runtime services it consumes."""
        del runtime
        return cls()

    @property
    def expression(self) -> str:
        return cast(str, getattr(settings, f"{self.name}_cron"))

    @property
    def enabled(self) -> bool:
        return cast(bool, getattr(settings, f"{self.name}_enabled"))

    def register_cron(
        self,
        worker: PgQueuer,
        callback: Callable[[Schedule], Awaitable[None]],
    ) -> None:
        """Register one enabled cron callback with PgQueuer."""
        if self.enabled:
            worker.schedule(self.cron_entrypoint, self.expression)(callback)

    @abc.abstractmethod
    def register(self, worker: PgQueuer, fan_out: FanOut) -> None:
        """Register the interfaces this job actually consumes."""


class ScopedScheduledJob(ScheduledJob, QueueJob[MaintenanceJob], abc.ABC):
    """Scheduled work fanned out through one durable queue item per scope set."""

    payload_type: ClassVar[type[QueuePayload]] = MaintenanceJob
    priority: ClassVar[int] = JobPriority.maintenance
    concurrency_limit: ClassVar[int] = 1
    entrypoint: ClassVar[str]

    def __init_subclass__(cls, **kwargs: bool) -> None:
        super().__init_subclass__(**kwargs)
        cls.entrypoint = f"aizk_task_{cls.name}"

    @abc.abstractmethod
    async def execute(self, scopes: Scopes) -> None:
        """Run this maintenance body for one exact scope set."""

    async def handle(self, payload: MaintenanceJob) -> None:
        await self.execute(frozenset(payload.scopes))

    async def fire_cron(self, fan_out: FanOut, schedule: Schedule) -> None:
        del schedule
        await fan_out(self)

    def register(self, worker: PgQueuer, fan_out: FanOut) -> None:
        self.bind(worker)
        self.register_cron(worker, partial(self.fire_cron, fan_out))


class SystemScheduledJob(ScheduledJob, abc.ABC):
    """Scheduled system work that runs once without a tenant scope queue."""

    @abc.abstractmethod
    async def execute(self) -> None:
        """Run this system-wide maintenance body once."""

    async def fire_cron(self, schedule: Schedule) -> None:
        del schedule
        await self.execute()

    def register(self, worker: PgQueuer, fan_out: FanOut) -> None:
        del fan_out
        self.register_cron(worker, self.fire_cron)


async def recorded_fact_count(session: Session, scopes: Scopes) -> int:
    """Count every recorded fact claim for a monotonic growth signal."""
    return (
        await session.exec(
            select(Fact.Claim.id.count())
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


class DecayJob(ScopedScheduledJob):
    """Archive stale facts past their half-life each day."""

    async def execute(self, scopes: Scopes) -> None:
        await decay(scopes=scopes, half_life_days=settings.decay_half_life_days)


class ArtifactDispatchJob(ScopedScheduledJob):
    """Recover accepted originals left pending by an interrupted queue handoff."""

    model_config = cast(
        "ConfigDict", {**FrozenModel.model_config, "arbitrary_types_allowed": True}
    )

    services: ArtifactServices

    @classmethod
    def assemble(cls, runtime: Runtime) -> Self:
        return cls(services=runtime.artifacts)

    async def execute(self, scopes: Scopes) -> None:
        await self.services.intake.dispatch_pending(scopes)


class ArtifactIntegrityJob(SystemScheduledJob):
    """Verify a bounded stale batch of immutable originals each day."""

    model_config = cast(
        "ConfigDict", {**FrozenModel.model_config, "arbitrary_types_allowed": True}
    )

    services: ArtifactServices

    @classmethod
    def assemble(cls, runtime: Runtime) -> Self:
        return cls(services=runtime.artifacts)

    async def execute(self) -> None:
        report = await self.services.integrity.verify(
            settings.artifact_integrity_batch_size,
            settings.artifact_integrity_interval_days,
        )
        logger.info(
            "artifact integrity checked {} objects, {} valid, {} failed",
            report.checked,
            report.valid,
            report.failed,
        )


class DedupJob(ScopedScheduledJob):
    """Merge duplicate entities each night."""

    async def execute(self, scopes: Scopes) -> None:
        await dedup_entities(scopes=scopes)


class CommunitiesJob(ScopedScheduledJob):
    """Rebuild communities after enough new facts arrive."""

    async def execute(self, scopes: Scopes) -> None:
        await run_if_grown(
            scopes,
            Watermark.Kind.fact_count,
            settings.communities_every_n_facts,
            partial(build_communities, scopes=scopes),
            "community",
        )


class ModelBackedJob(ScopedScheduledJob, abc.ABC):
    """A scheduled pass whose body consumes the runtime's generation and embedding
    clients."""

    model_config = cast(
        "ConfigDict", {**FrozenModel.model_config, "arbitrary_types_allowed": True}
    )

    llm: LLM
    embed: Embedder

    @classmethod
    def assemble(cls, runtime: Runtime) -> Self:
        return cls(llm=runtime.llm, embed=runtime.embed)


class RaptorJob(ModelBackedJob):
    """Rebuild the RAPTOR tree after enough new facts arrive."""

    async def execute(self, scopes: Scopes) -> None:
        await run_if_grown(
            scopes,
            Watermark.Kind.raptor_fact_count,
            settings.raptor_every_n_facts,
            partial(build_raptor, self.llm, self.embed, scopes=scopes),
            "raptor",
        )


class ProfileProjectionJob(ModelBackedJob):
    """Consume one bounded dirty-profile batch every minute."""

    async def execute(self, scopes: Scopes) -> None:
        await refresh_dirty_profiles(self.llm, self.embed, scopes=scopes)


class ProfileRefreshJob(ModelBackedJob):
    """Rebuild every profile on the weekly cadence."""

    async def execute(self, scopes: Scopes) -> None:
        await refresh_profiles(self.llm, self.embed, scopes=scopes)


class SessionPromoteJob(ScopedScheduledJob):
    """Promote aged working items into the graph."""

    async def execute(self, scopes: Scopes) -> None:
        await promote_sessions(scopes=scopes)


class InsightJob(ModelBackedJob):
    """Derive reflective observations over the graph each week."""

    async def execute(self, scopes: Scopes) -> None:
        await derive_insights(self.llm, self.embed, scopes=scopes)


class BackupJob(SystemScheduledJob):
    """Dump and prune the database on its configured cadence."""

    async def execute(self) -> None:
        report = await scheduled_backup()
        logger.info("scheduled backup wrote {} bytes to {}", report.bytes, report.path)
