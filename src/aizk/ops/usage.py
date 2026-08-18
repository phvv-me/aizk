from datetime import datetime

import pendulum
from patos import FrozenModel
from sqlalchemy import true
from sqlmodel import select

from ..config import settings
from ..status import UsagePoint, UsageSummary
from ..store import Usage
from ..store.backend import DatabaseRole, database_adapter
from .reports import ActorUsage, ScopeUsage

_PERIODS = (7, 30, 90, 365)


class PeriodUsage(FrozenModel):
    """One window the console offers and the totals measured over it."""

    days: int
    start: datetime
    summary: UsageSummary


class PlatformUsage(FrozenModel):
    """Durable usage across every scope at once, measured in one pass.

    Each offered window carries its own measured totals rather than a sum the browser
    computes, so what counts as an upload or a keep is decided once, beside the
    aggregate columns that define it, and never restated in the page.
    """

    generated_at: datetime
    periods: tuple[PeriodUsage, ...]
    lifetime: UsageSummary
    points: tuple[UsagePoint, ...]
    by_actor: tuple[ActorUsage, ...]
    by_scope: tuple[ScopeUsage, ...]


async def platform_usage() -> PlatformUsage:
    """Aggregate the whole usage ledger by window, by day, by caller, and by scope.

    `by_scope` groups on `targets`, the private or organization scope an operation touched,
    not on the row's own `scopes` column. That column is the RLS attribute every `UsageEvent`
    carries and `UsageAccountingJob` always writes as the acting caller's own private scope
    alone (`usage.py`'s `UsageCapture.event`), so it never holds an organization id and
    cannot answer "which organization".
    """
    generated_at = pendulum.now("UTC")
    starts = {days: generated_at.subtract(days=days - 1).start_of("day") for days in _PERIODS}
    targets = Usage.Event.targets.f.unnest().table_valued("scope_id").render_derived()
    by_actor = select(Usage.Event.created_by.label("actor_id"), *Usage.Event.aggregate()).group_by(
        Usage.Event.created_by
    )
    by_scope = (
        select(targets.c.scope_id, *Usage.Event.aggregate())
        .select_from(Usage.Event)
        .join(targets, true())
        .group_by(targets.c.scope_id)
    )
    admin = database_adapter().engine(settings.admin_database_url, DatabaseRole.owner)
    try:
        async with admin.connect() as connection:
            periods = [
                PeriodUsage(
                    days=days,
                    start=start,
                    summary=UsageSummary.model_validate(
                        (await connection.execute(Usage.Event.report_totals(start))).one(),
                        from_attributes=True,
                    ),
                )
                for days, start in starts.items()
            ]
            lifetime = (await connection.execute(Usage.Event.report_totals())).one()
            points = (
                await connection.execute(Usage.Event.daily_since(starts[max(_PERIODS)]))
            ).all()
            actor_rows = (await connection.execute(by_actor)).all()
            scope_rows = (await connection.execute(by_scope)).all()
    finally:
        await admin.dispose()
    return PlatformUsage(
        generated_at=generated_at,
        periods=tuple(periods),
        lifetime=UsageSummary.model_validate(lifetime, from_attributes=True),
        points=tuple(UsagePoint.model_validate(row, from_attributes=True) for row in points),
        by_actor=tuple(ActorUsage.model_validate(row, from_attributes=True) for row in actor_rows),
        by_scope=tuple(ScopeUsage.model_validate(row, from_attributes=True) for row in scope_rows),
    )
