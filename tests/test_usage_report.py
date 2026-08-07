from datetime import UTC, datetime
from typing import NamedTuple
from uuid import UUID

import dbutil
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
    """Reset storage and durably record four events across two actors, two orgs, three months."""
    await dbutil.reset_db()
    identity = Seed(uuid5(), uuid5(), uuid5(), uuid5())
    events = (
        capture(
            "e1",
            datetime(2026, 1, 1, tzinfo=UTC),
            identity.actor_a,
            Usage.Event.Operation.recall,
            (identity.actor_a,),
        ),
        capture(
            "e2",
            datetime(2026, 2, 1, tzinfo=UTC),
            identity.actor_a,
            Usage.Event.Operation.remember_text,
            (identity.org_x,),
        ),
        capture(
            "e3",
            datetime(2026, 2, 15, tzinfo=UTC),
            identity.actor_b,
            Usage.Event.Operation.recall,
            (identity.org_x,),
        ),
        capture(
            "e4",
            datetime(2026, 3, 1, tzinfo=UTC),
            identity.actor_b,
            Usage.Event.Operation.share,
            (identity.org_y,),
        ),
    )
    job = UsageAccountingJob()
    for event in events:
        await job.handle(event)
    return identity


def test_usage_report_aggregates_every_event_by_actor_and_organization() -> None:
    async def scenario() -> tuple[Seed, ops.UsageFilterReport]:
        identity = await seed()
        return identity, await ops.usage_report()

    identity, report = dbutil.run(scenario())

    by_actor = {row.actor_id: row for row in report.by_actor}
    assert set(by_actor) == {identity.actor_a, identity.actor_b}
    assert by_actor[identity.actor_a].recalls == 1
    assert by_actor[identity.actor_a].remembers == 1
    assert by_actor[identity.actor_b].recalls == 1
    assert by_actor[identity.actor_b].shares == 1
    by_scope = {row.scope_id: row for row in report.by_scope}
    assert set(by_scope) == {identity.actor_a, identity.org_x, identity.org_y}
    assert by_scope[identity.org_x].recalls == 1
    assert by_scope[identity.org_x].remembers == 1
    assert by_scope[identity.org_y].shares == 1
    assert (report.operation, report.actor_id, report.scope_id) == (None, None, None)


def test_usage_report_filters_by_operation() -> None:
    async def scenario() -> ops.UsageFilterReport:
        await seed()
        return await ops.usage_report(operation=Usage.Event.Operation.recall)

    report = dbutil.run(scenario())

    assert report.operation == Usage.Event.Operation.recall
    assert sum(row.recalls for row in report.by_actor) == 2
    assert sum(row.remembers for row in report.by_actor) == 0
    assert sum(row.shares for row in report.by_actor) == 0


def test_usage_report_filters_by_actor() -> None:
    async def scenario() -> tuple[Seed, ops.UsageFilterReport]:
        identity = await seed()
        return identity, await ops.usage_report(actor_id=identity.actor_a)

    identity, report = dbutil.run(scenario())

    assert [row.actor_id for row in report.by_actor] == [identity.actor_a]
    assert {row.scope_id for row in report.by_scope} == {identity.actor_a, identity.org_x}


def test_usage_report_filters_by_organization_scope() -> None:
    async def scenario() -> tuple[Seed, ops.UsageFilterReport]:
        identity = await seed()
        return identity, await ops.usage_report(scope_id=identity.org_x)

    identity, report = dbutil.run(scenario())

    assert [row.scope_id for row in report.by_scope] == [identity.org_x]
    assert {row.actor_id for row in report.by_actor} == {identity.actor_a, identity.actor_b}


def test_usage_report_filters_by_time_window() -> None:
    start = datetime(2026, 1, 15, tzinfo=UTC)
    end = datetime(2026, 2, 20, tzinfo=UTC)

    async def scenario() -> tuple[Seed, ops.UsageFilterReport]:
        identity = await seed()
        return identity, await ops.usage_report(start=start, end=end)

    identity, report = dbutil.run(scenario())

    by_scope = {row.scope_id for row in report.by_scope}
    assert identity.org_x in by_scope
    assert identity.org_y not in by_scope
    assert report.start == start
    assert report.end == end
