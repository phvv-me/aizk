import abc
import uuid
from collections.abc import Awaitable, Callable

from loguru import logger
from patos import FrozenModel, Registry
from pgqueuer import PgQueuer
from pgqueuer.models import Job, Schedule
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..eval import run_eval
from ..graph.build import dedup_entities
from ..graph.communities import build_communities
from ..graph.curation_review import review_curated_groups
from ..graph.decay import decay
from ..graph.insight import derive_insights
from ..graph.profiles import refresh_profiles
from ..graph.raptor import build_raptor
from ..graph.session_tier import promote_sessions
from ..store import LiveFact, Watermark, acting_as
from .payloads import TaskJob

FanOut = Callable[["ScheduledTask"], Awaitable[None]]


class ScheduledTask(Registry, FrozenModel, abc.ABC):
    """One background maintenance pass the scheduler fans out across the principals.

    A concrete subclass names itself with an explicit `name` matching its settings prefix
    (`decay`, `dedup`, ...) and implements `run`; `expression` and `enabled` then read straight off
    `{name}_cron`/`{name}_enabled` on the live settings, so a subclass carries no state of its own
    and `ScheduledTask.implementations()` is the whole roster of passes.

    Evaluated procrastinate (https://procrastinate.readthedocs.io) as a replacement for pgqueuer
    and kept pgqueuer. procrastinate's `@app.task(queueing_lock=)` matches pgqueuer's `dedupe_key`
    one for one, but two gaps rule it out: it ships no asyncpg connector, only
    `psycopg`/`Psycopg2Connector`/`AiopgConnector`, so adopting it means a second Postgres driver
    alongside the asyncpg one every other engine and connection here already shares; and
    `@app.periodic(cron=...)` fires its decorated task on a cron tick with a bare timestamp, with
    no notion of fanning that tick into one job per principal, so `schedule.fan_out`'s no-leak
    boundary would have to be hand-written again underneath it. Since the registry survives either
    way and the driver swap is a real cost with no offsetting code reduction, pgqueuer stays.
    """

    @property
    def queue_entrypoint(self) -> str:
        """Name of the queue entrypoint that runs this task's per-principal body."""
        return f"aizk_task_{self.name}"

    @property
    def cron_entrypoint(self) -> str:
        """Name of the cron entrypoint that fans this task out across the principals."""
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
    async def run(self, principal_id: uuid.UUID) -> None:
        """Run this task's per-principal body under acting_as that principal.

        principal_id: identity whose slice of the pass runs.
        """

    def register(self, pg: PgQueuer, fan_out: FanOut) -> None:
        """Register this task's queue entrypoint always, and its cron fan-out only when enabled.

        pg: the PgQueuer application the entrypoints attach to.
        fan_out: enqueues one per-principal job for this task across the roster.
        """

        @pg.entrypoint(self.queue_entrypoint)
        async def run_principal_job(job: Job) -> None:
            """Run this task's per-principal body under the principal its payload names.

            job: dequeued job whose payload names the principal.
            """
            assert job.payload is not None
            await self.run(TaskJob.decode(job.payload).principal_id)

        if not self.enabled:
            return

        @pg.schedule(self.cron_entrypoint, self.expression)
        async def fire(schedule: Schedule) -> None:
            """Fan this task out across the principal roster on its cron cadence.

            schedule: the cron schedule pgqueuer fired this run from.
            """
            await fan_out(self)


async def latest_fact_count(session: AsyncSession) -> int:
    """Count of latest facts, the growth signal both growth-gated passes measure against.

    session: an open session already acting as the pass's principal.
    """
    return await session.scalar(select(func.count()).select_from(LiveFact)) or 0


def config_from_label(label: str) -> dict[str, bool]:
    """Parse a swept-config label like `rerank=True,ppr=False` into a settings override dict.

    The inverse of the label run_eval keys each toggle run under, so the axis a significant win
    names becomes the rerank-and-ppr override the self-improve pass persists for recall to read.

    label: the comma-separated `axis=Bool` label of the winning sweep.
    """
    overrides: dict[str, bool] = {}
    for pair in label.split(","):
        axis, _, value = pair.partition("=")
        overrides[axis] = value == "True"
    return overrides


class DecayTask(ScheduledTask):
    """Archive stale facts past their half-life, the daily decay pass."""

    name = "decay"

    async def run(self, principal_id: uuid.UUID) -> None:
        await decay(principal_id=principal_id, half_life_days=settings.decay_half_life_days)


class DedupTask(ScheduledTask):
    """Merge duplicate entities, the nightly dedup pass."""

    name = "dedup"

    async def run(self, principal_id: uuid.UUID) -> None:
        await dedup_entities(principal_id=principal_id)


class CommunitiesTask(ScheduledTask):
    """Rebuild communities once the graph grew by `communities_every_n_facts`, the weekly gate."""

    name = "communities"

    async def run(self, principal_id: uuid.UUID) -> None:
        async with acting_as(principal_id) as session:
            current = await latest_fact_count(session)
            last = await Watermark.read(session, principal_id, Watermark.Kind.fact_count)
        if current - last < settings.communities_every_n_facts:
            logger.info(
                "community pass skipped for {}, {} new facts", principal_id, current - last
            )
            return
        await build_communities(principal_id=principal_id)
        async with acting_as(principal_id) as session:
            await Watermark.set_value(
                session, principal_id, Watermark.Kind.fact_count, counter=current
            )


class RaptorTask(ScheduledTask):
    """Rebuild the RAPTOR tree once the graph grew by `raptor_every_n_facts`, the RAPTOR gate."""

    name = "raptor"

    async def run(self, principal_id: uuid.UUID) -> None:
        async with acting_as(principal_id) as session:
            current = await latest_fact_count(session)
            last = await Watermark.read(session, principal_id, Watermark.Kind.raptor_fact_count)
        if current - last < settings.raptor_every_n_facts:
            logger.info("raptor pass skipped for {}, {} new facts", principal_id, current - last)
            return
        await build_raptor(principal_id=principal_id)
        async with acting_as(principal_id) as session:
            await Watermark.set_value(
                session, principal_id, Watermark.Kind.raptor_fact_count, counter=current
            )


class ProfileRefreshTask(ScheduledTask):
    """Rebuild every profile, the weekly full refresh pass."""

    name = "profile_refresh"

    async def run(self, principal_id: uuid.UUID) -> None:
        await refresh_profiles(principal_id=principal_id)


class SelfImproveTask(ScheduledTask):
    """Score recall, store the scorecard, and flip on a significant win, the weekly self-eval pass.

    A significant win flips the live `settings` singleton in-process, the EvolveMem adaptive loop,
    but only for this process's lifetime since env stays the durable config across a restart; no
    significant win means nothing is flipped, so the live config never moves on noise.
    """

    name = "self_improve"

    async def run(self, principal_id: uuid.UUID) -> None:
        report = await run_eval(None, principal_id=principal_id)
        best = (
            max(report.per_config, key=lambda label: report.per_config[label])
            if report.per_config
            else None
        )
        async with acting_as(principal_id) as session:
            await Watermark.set_value(
                session,
                principal_id,
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
        if report.significant_best is not None:
            flip = config_from_label(report.significant_best)
            for axis, value in flip.items():
                setattr(settings, axis, value)
            logger.info("self-improve flipped {} for {} in-process", flip, principal_id)
        logger.info(
            "self-improve scored {} items for {}, best {}, flipped {}",
            report.n,
            principal_id,
            best,
            report.significant_best,
        )


class SessionPromoteTask(ScheduledTask):
    """Promote aged working items into the graph, the quarter-hour promotion pass."""

    name = "session_promote"

    async def run(self, principal_id: uuid.UUID) -> None:
        await promote_sessions(principal_id=principal_id)


class InsightTask(ScheduledTask):
    """Derive reflective observations over the graph, the weekly insight pass."""

    name = "insight"

    async def run(self, principal_id: uuid.UUID) -> None:
        await derive_insights(principal_id=principal_id)


class CurationReviewTask(ScheduledTask):
    """Judge every curated group a principal administers, the weekly standing-reviewer pass.

    A principal earns this pass's attention purely by holding the admin membership role in a
    curated group, human or a dedicated agent identity added as an admin member for exactly this
    purpose, the fan-out's own per-principal scoping already the only "which identity reviews
    this group" signal the system needs.
    """

    name = "curation_review"

    async def run(self, principal_id: uuid.UUID) -> None:
        await review_curated_groups(principal_id=principal_id)
