from datetime import UTC, datetime, timedelta

import dbutil
import pytest
import seedgraph
from sqlalchemy.dialects.postgresql import Range

from aizk.graph.timeline import ProjectSummary, TimelineEntry, projects, timeline
from aizk.store import Profile, acting_as

pytestmark = pytest.mark.usefixtures("migrated_db")


def test_timeline_entry_renders_a_dated_line() -> None:
    """`TimelineEntry.render` is `YYYY-MM-DD (predicate) statement`, the weekly-review log line."""
    entry = TimelineEntry(
        recorded=datetime(2024, 3, 5, 14, tzinfo=UTC), predicate="uses", statement="Ada uses it"
    )
    assert entry.render() == "2024-03-05 (uses) Ada uses it"


def test_project_summary_renders_title_profile_and_recent_lines() -> None:
    """`ProjectSummary.render` stacks the name, its profile, and each recent entry as a line."""
    entry = TimelineEntry(
        recorded=datetime(2024, 3, 5, tzinfo=UTC), predicate="uses", statement="s"
    )
    with_profile = ProjectSummary(name="Spine", profile="a portrait", recent=[entry]).render()
    assert with_profile == "Spine\n  profile: a portrait\n  - 2024-03-05 (uses) s"
    blank = ProjectSummary(name="Spine", profile=None, recent=[]).render()
    assert blank == "Spine\n  profile: no profile yet"


def test_timeline_reads_the_window_newest_first_and_filters_by_entity() -> None:
    """`timeline` returns claims recorded inside the trailing window, newest first, entity-named.

    A claim recorded weeks ago falls outside the seven-day window and never surfaces, the two
    in-window claims come back newest transaction-time first, and an entity substring narrows the
    read to facts whose subject or object name matches.
    """

    async def body() -> tuple[list[str], list[str]]:
        owner = await seedgraph.fresh_owner()
        now = datetime.now(UTC)
        async with acting_as(owner) as session:
            ada = await seedgraph.add_entity(session, owner, "Ada Lovelace", type="Author")
            other = await seedgraph.add_entity(session, owner, "Somebody Else", type="Author")
            await seedgraph.add_fact(
                session, owner, ada, statement="recent about ada", recorded=Range(now, None)
            )
            await seedgraph.add_fact(
                session,
                owner,
                ada,
                statement="older about ada",
                recorded=Range(now - timedelta(hours=1), None),
            )
            await seedgraph.add_fact(
                session,
                owner,
                other,
                statement="ancient about other",
                recorded=Range(now - timedelta(days=30), now - timedelta(days=20)),
            )
        window = await timeline(owner, since_days=7.0)
        named = await timeline(owner, since_days=7.0, entity="ada")
        return [entry.statement for entry in window], [entry.statement for entry in named]

    window_statements, named_statements = dbutil.run(body())
    assert window_statements == ["recent about ada", "older about ada"]  # newest first, no ancient
    assert named_statements == ["recent about ada", "older about ada"]  # only ada's own facts


def test_projects_lists_each_project_with_profile_and_recent_facts() -> None:
    """`projects` returns every visible Project entity, its profile, and its most recent facts.

    A Project-typed entity with a rolled-up profile and two facts surfaces both, capped to
    `recent_k`, while a non-Project entity never appears in the roster.
    """

    async def body() -> list[ProjectSummary]:
        owner = await seedgraph.fresh_owner()
        async with acting_as(owner) as session:
            spine = await seedgraph.add_entity(session, owner, "Spine", type="Project")
            await seedgraph.add_entity(session, owner, "Not A Project", type="Concept")
            session.add(Profile(owner_id=owner, subject_id=spine, summary="the spine portrait"))
            await seedgraph.add_fact(session, owner, spine, statement="spine fact one")
            await seedgraph.add_fact(session, owner, spine, statement="spine fact two")
        return await projects(owner, recent_k=3)

    summaries = dbutil.run(body())
    assert [summary.name for summary in summaries] == ["Spine"]  # only the Project entity
    spine = summaries[0]
    assert spine.profile == "the spine portrait"
    assert {entry.statement for entry in spine.recent} == {"spine fact one", "spine fact two"}


def test_projects_reports_no_profile_when_none_has_been_built() -> None:
    """A Project with no profile row surfaces with a null profile, the pre-rollup state."""

    async def body() -> ProjectSummary | None:
        owner = await seedgraph.fresh_owner()
        async with acting_as(owner) as session:
            await seedgraph.add_entity(session, owner, "Bare", type="Project")
        summaries = await projects(owner)
        return summaries[0] if summaries else None

    summary = dbutil.run(body())
    assert summary is not None
    assert summary.profile is None
    assert summary.recent == []
