import abc
import uuid
from collections.abc import Awaitable, Callable
from functools import partial

from loguru import logger
from patos import FrozenModel, Registry
from pgqueuer import PgQueuer
from pgqueuer.models import Job, Schedule
from sqlalchemy import func, select

from ..backup import scheduled_backup
from ..config import settings
from ..eval import EvalReport, run_eval
from ..graph.build import dedup_entities
from ..graph.communities import build_communities
from ..graph.curation_review import review_curated_groups
from ..graph.decay import decay
from ..graph.insight import derive_insights
from ..graph.profiles import refresh_profiles
from ..graph.raptor import build_raptor
from ..graph.session_tier import promote_sessions
from ..store import FactClaim, Watermark, acting_as
from ..store.engine import session
from .payloads import TaskJob

FanOut = Callable[["ScheduledTask"], Awaitable[None]]


class ScheduledTask(Registry, FrozenModel, abc.ABC):
    """One background maintenance pass the scheduler fans out across the users.

    A concrete subclass names itself with an explicit `name` matching its settings prefix
    (`decay`, `dedup`, ...) and implements `run`. `expression` and `enabled` then read straight off
    `{name}_cron`/`{name}_enabled` on the live settings, so a subclass carries no state of its own
    and `ScheduledTask.implementations()` is the whole roster of passes.

    Evaluated procrastinate (https://procrastinate.readthedocs.io) as a replacement for pgqueuer
    and kept pgqueuer. procrastinate's `@app.task(queueing_lock=)` matches pgqueuer's `dedupe_key`
    one for one, but two gaps rule it out. It ships no asyncpg connector, only
    `psycopg`/`Psycopg2Connector`/`AiopgConnector`, so adopting it means a second Postgres driver
    alongside the asyncpg one every other engine and connection here already shares. Its
    `@app.periodic(cron=...)` also fires its decorated task on a cron tick with a bare timestamp,
    with no notion of fanning that tick into one job per user, so `schedule.fan_out`'s no-leak
    boundary would have to be hand-written again underneath it. Since the registry survives either
    way and the driver swap is a real cost with no offsetting code reduction, pgqueuer stays.
    """

    @property
    def queue_entrypoint(self) -> str:
        """Name of the queue entrypoint that runs this task's per-user body."""
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
    async def run(self, user_id: uuid.UUID) -> None:
        """Run this task's per-user body under acting_as that user.

        user_id: identity whose slice of the pass runs.
        """

    async def run_job(self, job: Job) -> None:
        """Run this task's per-user body under the user its dequeued payload names.

        job: dequeued job whose payload names the user.
        """
        assert job.payload is not None
        await self.run(TaskJob.decode(job.payload).user_id)

    async def fire_cron(self, fan_out: FanOut, schedule: Schedule) -> None:
        """Fan this task out across the user roster on its cron cadence.

        fan_out: enqueues one per-user job for this task across the roster.
        schedule: the cron schedule pgqueuer fired this run from, unused past selecting this task.
        """
        await fan_out(self)

    def register(self, pg: PgQueuer, fan_out: FanOut) -> None:
        """Register this task's queue entrypoint always, and its cron fan-out only when enabled.

        pg: the PgQueuer application the entrypoints attach to.
        fan_out: enqueues one per-user job for this task across the roster.
        """
        pg.entrypoint(self.queue_entrypoint)(self.run_job)
        if not self.enabled:
            return
        pg.schedule(self.cron_entrypoint, self.expression)(partial(self.fire_cron, fan_out))


async def recorded_fact_count() -> int:
    """Count of every fact claim ever recorded, the monotonic growth signal the gated passes read.

    Counts the whole claim history with the live gate off, so a superseded, decayed, or forgotten
    version still counts, since each of those only closes a claim's `recorded` upper bound and
    never deletes its row. So this measure only ever climbs, unlike a live-fact count that a
    decaying or forgetting graph drives back down. That lets a churning graph, one that adds far
    more than it retires yet stays near net-flat, still clear the growth threshold and rebuild its
    summaries rather than serving permanently stale ones because its live count barely moved.
    """
    return (
        await session().scalar(
            select(func.count())
            .select_from(FactClaim)
            .execution_options(**{settings.skip_live_gate: True})
        )
        or 0
    )


async def run_if_grown(
    user_id: uuid.UUID,
    kind: Watermark.Kind,
    threshold: int,
    build: Callable[[], Awaitable[None]],
    label: str,
) -> None:
    """Run a growth-gated rebuild only once the graph grew by threshold facts since its watermark.

    The shared body of `CommunitiesTask` and `RaptorTask`, whose only difference is which watermark
    kind gates them, how large a growth threshold they wait for, and which builder they run. Reads
    the cumulative recorded fact count once, a measure that only climbs so structural churn always
    eventually clears the gate. Below the threshold it only logs and returns, otherwise it runs the
    builder and advances the watermark to the count just measured.

    user_id: identity whose slice of the pass runs.
    kind: the watermark kind the growth is measured and persisted against.
    threshold: facts of growth required before the builder runs again.
    build: the zero-argument rebuild to run once growth clears the threshold.
    label: the pass name logged when growth has not yet cleared the threshold.
    """
    async with acting_as(user_id):
        current = await recorded_fact_count()
        last = await Watermark.read(user_id, kind)
    if current - last < threshold:
        logger.info("{} pass skipped for {}, {} new facts", label, user_id, current - last)
        return
    await build()
    async with acting_as(user_id):
        await Watermark.set_value(user_id, kind, counter=current)


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


def per_config_best(per_config: dict[str, float]) -> str | None:
    """The swept config label with the highest score, null when nothing was scored.

    per_config: hit-at-k keyed by the rerank/ppr (multi-hop personalized-pagerank) toggle label,
        EvalReport's own per_config field.
    """
    return max(per_config, key=lambda label: per_config[label]) if per_config else None


async def store_scorecard(user_id: uuid.UUID, report: EvalReport, best: str | None) -> None:
    """Persist the weekly self-eval scorecard as the scorecard watermark's payload.

    user_id: identity the scorecard is stored under.
    report: the freshly scored recall-quality report.
    best: the argmax over report.per_config, null when nothing was scored.
    """
    await Watermark.set_value(
        user_id,
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


def apply_significant_win(significant_best: str | None) -> dict[str, bool]:
    """Flip the live settings singleton to a significant sweep win, the EvolveMem adaptive loop.

    Mutates `settings` in-process only. Env stays the durable config across a restart. Returns the
    flipped fields, empty when there was no significant win to apply.

    significant_best: the swept config label that beat the current one, null when none did.
    """
    if significant_best is None:
        return {}
    flip = config_from_label(significant_best)
    for axis, value in flip.items():
        setattr(settings, axis, value)
    return flip


class DecayTask(ScheduledTask):
    """Archive stale facts past their half-life, the daily decay pass."""

    name = "decay"

    async def run(self, user_id: uuid.UUID) -> None:
        await decay(user_id=user_id, half_life_days=settings.decay_half_life_days)


class DedupTask(ScheduledTask):
    """Merge duplicate entities, the nightly dedup pass."""

    name = "dedup"

    async def run(self, user_id: uuid.UUID) -> None:
        await dedup_entities(user_id=user_id)


class CommunitiesTask(ScheduledTask):
    """Rebuild communities, clusters of related entities detected from the fact graph, once it
    grew by `communities_every_n_facts` facts, the weekly gate.
    """

    name = "communities"

    async def run(self, user_id: uuid.UUID) -> None:
        await run_if_grown(
            user_id,
            Watermark.Kind.fact_count,
            settings.communities_every_n_facts,
            partial(build_communities, user_id=user_id),
            "community",
        )


class RaptorTask(ScheduledTask):
    """Rebuild the RAPTOR tree, the hierarchical summary tree recall reads for thematic queries,
    once the graph grew by `raptor_every_n_facts` facts, the RAPTOR gate.
    """

    name = "raptor"

    async def run(self, user_id: uuid.UUID) -> None:
        await run_if_grown(
            user_id,
            Watermark.Kind.raptor_fact_count,
            settings.raptor_every_n_facts,
            partial(build_raptor, user_id=user_id),
            "raptor",
        )


class ProfileRefreshTask(ScheduledTask):
    """Rebuild every profile, the weekly full refresh pass."""

    name = "profile_refresh"

    async def run(self, user_id: uuid.UUID) -> None:
        await refresh_profiles(user_id=user_id)


class SelfImproveTask(ScheduledTask):
    """Score recall, store the scorecard, and flip on a significant win, the weekly self-eval pass.

    A significant win flips the live `settings` singleton in-process, the EvolveMem adaptive loop,
    but only for this process's lifetime since env stays the durable config across a restart. No
    significant win means nothing is flipped, so the live config never moves on noise.
    """

    name = "self_improve"

    async def run(self, user_id: uuid.UUID) -> None:
        report = await run_eval(None, user_id=user_id)
        best = per_config_best(report.per_config)
        async with acting_as(user_id):
            await store_scorecard(user_id, report, best)
        flip = apply_significant_win(report.significant_best)
        if flip:
            logger.info("self-improve flipped {} for {} in-process", flip, user_id)
        logger.info(
            "self-improve scored {} items for {}, best {}, flipped {}",
            report.n,
            user_id,
            best,
            report.significant_best,
        )


class SessionPromoteTask(ScheduledTask):
    """Promote aged working items into the graph, the quarter-hour promotion pass."""

    name = "session_promote"

    async def run(self, user_id: uuid.UUID) -> None:
        await promote_sessions(user_id=user_id)


class InsightTask(ScheduledTask):
    """Derive reflective observations over the graph, the weekly insight pass."""

    name = "insight"

    async def run(self, user_id: uuid.UUID) -> None:
        await derive_insights(user_id=user_id)


class CurationReviewTask(ScheduledTask):
    """Judge every curated group a user administers, the weekly standing-reviewer pass.

    A user earns this pass's attention purely by holding the admin membership role in a
    curated group, human or a dedicated agent identity added as an admin member for exactly this
    purpose, the fan-out's own per-user scoping already the only "which identity reviews
    this group" signal the system needs.
    """

    name = "curation_review"

    async def run(self, user_id: uuid.UUID) -> None:
        await review_curated_groups(user_id=user_id)


class BackupTask(ScheduledTask):
    """Dump the whole database on a cron, the integrated auto-backup pass the worker runs.

    The one pass that fires once globally rather than fanned out per user, since a backup is
    a single owner-level `pg_dump` capturing every tenant's rows at once, not a per-user
    graph operation. So it overrides the fan-out cron to run the dump directly and registers only
    that cron, never the per-user queue entrypoint every maintenance pass otherwise carries.
    Off unless `backup_enabled` is set, so a plain host run never dumps while the container turns
    it on and mounts `backup_dir`.
    """

    name = "backup"

    async def run(self, user_id: uuid.UUID) -> None:
        raise NotImplementedError("backup is a system pass, never fanned out per user")

    async def fire_cron(self, fan_out: FanOut, schedule: Schedule) -> None:
        """Dump and prune directly on the cron tick, no per-user fan-out.

        fan_out: unused, a backup reads no user roster.
        schedule: the cron schedule this run fired from, unused past selecting this task.
        """
        report = await scheduled_backup()
        logger.info("scheduled backup wrote {} bytes to {}", report.bytes, report.path)

    def register(self, pg: PgQueuer, fan_out: FanOut) -> None:
        """Register only the backup cron, and only when enabled, no per-user entrypoint.

        pg: the PgQueuer application the cron attaches to.
        fan_out: unused, a backup never fans out.
        """
        if self.enabled:
            pg.schedule(self.cron_entrypoint, self.expression)(partial(self.fire_cron, fan_out))
