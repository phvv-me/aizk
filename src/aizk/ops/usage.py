from datetime import UTC, datetime

from patos import FrozenModel
from pydantic import UUID5
from sqlalchemy import true
from sqlmodel import select

from ..config import settings
from ..store import Usage, UsageEvent
from ..store.backend import database_adapter
from .reports import ActorUsage, ScopeUsage


class UsageFilterReport(FrozenModel):
    """Durable usage aggregated by actor and by organization scope under one composed filter.

    Every column an operator can filter on, kind, actor, organization scope, and the time
    window, is indexed, so the same predicate set narrows both breakdowns in one query pass
    rather than loading rows and summing them in Python.
    """

    generated_at: datetime
    operation: UsageEvent.Operation | None
    actor_id: UUID5 | None
    scope_id: UUID5 | None
    start: datetime | None
    end: datetime | None
    by_actor: tuple[ActorUsage, ...]
    by_scope: tuple[ScopeUsage, ...]


async def usage_report(
    operation: UsageEvent.Operation | None = None,
    actor_id: UUID5 | None = None,
    scope_id: UUID5 | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> UsageFilterReport:
    """Aggregate durable usage by actor and by organization scope under one composed filter.

    `scope_id` filters on `targets`, the private-or-organization scope an operation touched,
    not on the row's own `scopes` column. That column is the RLS attribute every `UsageEvent`
    carries and `UsageAccountingJob` always writes as the acting caller's own private scope
    alone (`usage.py`'s `UsageCapture.event`), so it never holds an organization id and cannot
    answer "which organization". `targets` is also what the existing `usage_health` scope
    breakdown already unnests, so this stays consistent with that established reading.
    """
    predicates = [
        *([Usage.Event.operation == operation] if operation is not None else []),
        *([Usage.Event.created_by == actor_id] if actor_id is not None else []),
        *([Usage.Event.targets.overlap([scope_id])] if scope_id is not None else []),
        *([Usage.Event.created_at >= start] if start is not None else []),
        *([Usage.Event.created_at < end] if end is not None else []),
    ]
    by_actor_statement = (
        select(Usage.Event.created_by.label("actor_id"), *Usage.Event.aggregate())
        .where(*predicates)
        .group_by(Usage.Event.created_by)
    )
    targets = Usage.Event.targets.f.unnest().table_valued("scope_id").render_derived()
    by_scope_statement = (
        select(targets.c.scope_id, *Usage.Event.aggregate())
        .select_from(Usage.Event)
        .join(targets, true())
        .where(*predicates)
        .group_by(targets.c.scope_id)
    )
    admin = database_adapter().engine(settings.admin_database_url, False)
    try:
        async with admin.connect() as connection:
            actor_rows = (await connection.execute(by_actor_statement)).all()
            scope_rows = (await connection.execute(by_scope_statement)).all()
    finally:
        await admin.dispose()
    return UsageFilterReport(
        generated_at=datetime.now(UTC),
        operation=operation,
        actor_id=actor_id,
        scope_id=scope_id,
        start=start,
        end=end,
        by_actor=tuple(ActorUsage.model_validate(row, from_attributes=True) for row in actor_rows),
        by_scope=tuple(ScopeUsage.model_validate(row, from_attributes=True) for row in scope_rows),
    )
