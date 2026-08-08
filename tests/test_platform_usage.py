from datetime import datetime
from typing import NamedTuple
from uuid import UUID

import dbutil
import pendulum
from id_factory import uuid5

import aizk.ops as ops
from aizk.store import Usage
from aizk.usage import UsageAccountingJob, UsageCapture


class Seed(NamedTuple):
    """The actor and organization identities four seeded events touch."""

    actor_a: UUID
    actor_b: UUID
    org_x: UUID
    org_y: UUID


def capture(
    key: str,
    occurred_at: datetime,
    actor: UUID,
    operation: Usage.Event.Operation,
    targets: tuple[UUID, ...],
) -> UsageCapture:
    """Build one durable capture for one seeded event."""
    return UsageCapture(
        capture_key=key,
        occurred_at=occurred_at,
        user_id=actor,
        operation=operation,
        targets=targets,
    )


async def seed() -> Seed:
    """Reset storage and record four events across two actors, two orgs, and four windows.

    The ages are relative to now rather than fixed dates, so each event falls inside exactly
    the windows it should whenever the suite runs.
    """
    await dbutil.reset_db()
    identity = Seed(uuid5(), uuid5(), uuid5(), uuid5())
    now = pendulum.now("UTC")
    events = (
        capture(
            "e1",
            now.subtract(days=2),
            identity.actor_a,
            Usage.Event.Operation.recall,
            (identity.actor_a,),
        ),
        capture(
            "e2",
            now.subtract(days=10),
            identity.actor_a,
            Usage.Event.Operation.remember_text,
            (identity.org_x,),
        ),
        capture(
            "e3",
            now.subtract(days=200),
            identity.actor_b,
            Usage.Event.Operation.recall,
            (identity.org_x,),
        ),
        capture(
            "e4",
            now.subtract(days=500),
            identity.actor_b,
            Usage.Event.Operation.share,
            (identity.org_y,),
        ),
    )
    job = UsageAccountingJob()
    for event in events:
        await job.handle(event)
    return identity


def measure() -> tuple[Seed, ops.PlatformUsage]:
    async def scenario() -> tuple[Seed, ops.PlatformUsage]:
        identity = await seed()
        return identity, await ops.platform_usage()

    return dbutil.run(scenario())


def test_platform_usage_attributes_every_event_to_its_actor_and_its_target_scopes() -> None:
    """The breakdowns cross every scope, which is the whole reason the owner measures them."""
    identity, usage = measure()

    by_actor = {row.actor_id: row for row in usage.by_actor}
    assert set(by_actor) == {identity.actor_a, identity.actor_b}
    assert by_actor[identity.actor_a].recalls == 1
    assert by_actor[identity.actor_a].remembers == 1
    assert by_actor[identity.actor_b].recalls == 1
    assert by_actor[identity.actor_b].shares == 1
    by_scope = {row.scope_id: row for row in usage.by_scope}
    assert set(by_scope) == {identity.actor_a, identity.org_x, identity.org_y}
    assert by_scope[identity.org_x].recalls == 1
    assert by_scope[identity.org_x].remembers == 1
    assert by_scope[identity.org_y].shares == 1


def test_every_offered_window_carries_its_own_measured_totals() -> None:
    """The console changes period by selecting a measured window, never by asking again."""
    _, usage = measure()

    requests = {period.days: period.summary.requests for period in usage.periods}

    assert requests == {7: 1, 30: 2, 90: 2, 365: 3}
    assert usage.lifetime.requests == 4


def test_the_daily_series_covers_the_longest_window_the_console_offers() -> None:
    """Shorter periods filter these buckets, so the series must span the widest one."""
    _, usage = measure()

    longest = max(period.days for period in usage.periods)
    start = next(period.start for period in usage.periods if period.days == longest)

    assert len(usage.points) == 3
    assert all(point.bucket >= start for point in usage.points)
    assert sum(point.requests for point in usage.points) == 3
