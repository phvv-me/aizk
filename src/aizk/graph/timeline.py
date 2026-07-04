import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from patos import FrozenModel
from sqlalchemy import Row, func, or_, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..extract.ontology import EntityType
from ..store import EntityContent, FactClaim, FactContent, Profile, acting_as


class TimelineEntry(FrozenModel):
    """One dated line of the weekly-review timeline, a fact as an event.

    recorded: transaction-time this claim was written, the moment it entered memory.
    predicate: ontology relation type the fact asserts.
    statement: self-contained natural-language rendering of the fact.
    """

    recorded: datetime
    predicate: str
    statement: str

    def render(self) -> str:
        """This entry as one dated line, `YYYY-MM-DD (predicate) statement`."""
        return f"{self.recorded.date().isoformat()} ({self.predicate}) {self.statement}"


class ProjectSummary(FrozenModel):
    """One Project-type entity's portrait, its profile beside its most recent timeline entries.

    name: the project's canonical name.
    profile: the rolled-up static-plus-dynamic summary, null until one has been built.
    recent: the project's most recent timeline entries, newest first.
    """

    name: str
    profile: str | None
    recent: list[TimelineEntry]

    def render(self) -> str:
        """This project as a title line, its profile, and its recent dated lines."""
        lines = [self.name, f"  profile: {self.profile or 'no profile yet'}"]
        lines += [f"  - {entry.render()}" for entry in self.recent]
        return "\n".join(lines)


def timeline_entries(rows: Sequence[Row]) -> list[TimelineEntry]:
    """Render a (statement, predicate, recorded) result into timeline entries.

    rows: a result selecting statement, predicate, and recorded, timeline's own query and
        projects' per-entity query alike.
    """
    return [
        TimelineEntry(
            recorded=row.recorded.lower, predicate=row.predicate, statement=row.statement
        )
        for row in rows
    ]


async def timeline(
    principal_id: uuid.UUID,
    since_days: float = 7.0,
    entity: str | None = None,
    scopes: tuple[uuid.UUID, ...] = (),
) -> list[TimelineEntry]:
    """List the claims recorded in the last since_days, newest first, the weekly-review read.

    Facts are the events table: a claim's own recorded transaction-time range is the log line,
    so this reads every claim (live or since superseded, opting out of the live gate the way any
    full-history read does) whose recorded range overlaps the trailing window, regardless of the
    fact's own valid-time. A note's dated journal lines (extract.journal) are exactly this shape,
    but any fact recorded in the window surfaces, journal-sourced or not.

    principal_id: identity whose row level security visibility scopes the read.
    since_days: how many trailing days to read, a week by default.
    entity: when set, only facts whose subject or object name matches this substring.
    scopes: group ids narrowing the read to that combination's composed graph, the whole visible
        union when empty.
    """
    now = datetime.now(UTC)
    window = Range(now - timedelta(days=since_days), now)
    async with acting_as(principal_id, scopes) as session:
        query = (
            select(FactContent.statement, FactContent.predicate, FactClaim.recorded)
            .join(FactContent, FactContent.id == FactClaim.content_id)
            .where(FactClaim.recorded.op("&&")(window))
            .order_by(func.lower(FactClaim.recorded).desc())
            .execution_options(**{settings.skip_live_gate: True})
        )
        if entity:
            named = select(EntityContent.id).where(EntityContent.name.ilike(f"%{entity}%"))
            query = query.where(
                or_(FactContent.subject_id.in_(named), FactContent.object_id.in_(named))
            )
        rows = await session.execute(query)
        return timeline_entries(rows.all())


async def project_roster(session: AsyncSession) -> Sequence[Row]:
    """Every visible Project-type entity with its rolled-up profile, name order.

    session: open, principal- and scope-scoped session the caller already holds.
    """
    return (
        await session.execute(
            select(EntityContent.id, EntityContent.name, Profile.summary)
            .outerjoin(Profile, Profile.subject_id == EntityContent.id)
            .where(EntityContent.type == EntityType.PROJECT)
            .order_by(EntityContent.name)
        )
    ).all()


async def recent_project_facts(
    session: AsyncSession, subject_id: uuid.UUID, recent_k: int
) -> list[TimelineEntry]:
    """One project's most recent timeline entries, newest first.

    session: open, principal- and scope-scoped session the caller already holds.
    subject_id: the project entity whose facts are read.
    recent_k: how many entries to return.
    """
    rows = await session.execute(
        select(FactContent.statement, FactContent.predicate, FactClaim.recorded)
        .join(FactContent, FactContent.id == FactClaim.content_id)
        .where(FactContent.subject_id == subject_id)
        .order_by(func.lower(FactClaim.recorded).desc())
        .limit(recent_k)
        .execution_options(**{settings.skip_live_gate: True})
    )
    return timeline_entries(rows.all())


async def projects(
    principal_id: uuid.UUID,
    scopes: tuple[uuid.UUID, ...] = (),
    recent_k: int = 3,
) -> list[ProjectSummary]:
    """List every visible Project-type entity with its profile and its most recent timeline facts.

    principal_id: identity whose row level security visibility scopes the read.
    scopes: group ids narrowing the read to that combination's composed graph, the whole visible
        union when empty.
    recent_k: how many of each project's most recent timeline entries to include.
    """
    async with acting_as(principal_id, scopes) as session:
        roster = await project_roster(session)
        return [
            ProjectSummary(
                name=name,
                profile=profile_summary,
                recent=await recent_project_facts(session, subject_id, recent_k),
            )
            for subject_id, name, profile_summary in roster
        ]
