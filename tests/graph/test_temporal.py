import uuid
from datetime import UTC, datetime, timedelta

import dbutil
import pytest
import seedgraph
from hypothesis import given
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import Range
from sqlmodel import select
from strategies import TemporalState, fact_timeline, temporal_states

from aizk.config import settings
from aizk.store import FactClaim, FactContent, LiveFact

pytestmark = pytest.mark.usefixtures("migrated_db")

GATE_OFF = {settings.skip_live_gate: True}


def test_visible_at_lists_the_live_gate_for_now_and_two_bounds_for_a_replay() -> None:
    assert len(FactClaim.visible_at(None)) == 1
    assert len(FactClaim.visible_at(datetime(2020, 1, 1, tzinfo=UTC))) == 2


@given(state=temporal_states())
def test_is_current_matches_the_temporal_spec(state: TemporalState) -> None:
    now = datetime.now(UTC)
    claim = FactClaim(
        content_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        valid=state.valid(now),
        recorded=state.recorded(now),
    )
    assert claim.is_current is state.expected_current(now)


def _recorded_holds(state: TemporalState, as_of: datetime, now: datetime) -> bool:
    lower = now - timedelta(days=1)
    upper = None if state.is_latest else now
    return lower <= as_of and (upper is None or as_of < upper)


def _valid_holds(state: TemporalState, as_of: datetime, now: datetime) -> bool:
    start, end = state.window(now)
    return (start is None or start <= as_of) and (end is None or as_of < end)


async def read_versions(
    states: list[TemporalState], now: datetime, as_of: datetime
) -> tuple[set[str], set[str], set[str], set[str]]:
    owner = await seedgraph.fresh_owner()
    async with dbutil.actor(owner) as session:
        subject = await seedgraph.add_entity(session, owner, "Subject")
        live_expected: set[str] = set()
        replay_expected: set[str] = set()
        for index, state in enumerate(states):
            statement = f"version {index}"
            if state.expected_current(now):
                live_expected.add(statement)
            if _recorded_holds(state, as_of, now) and _valid_holds(state, as_of, now):
                replay_expected.add(statement)
            await seedgraph.add_fact(
                session,
                owner,
                subject,
                statement=statement,
                valid=state.valid(now),
                recorded=state.recorded(now),
            )
    claims = (
        select(FactContent.statement)
        .join(FactClaim, FactClaim.content_id == FactContent.id)
        .where(FactContent.subject_id == subject)
    )
    async with dbutil.actor(owner) as session:
        live = set(await session.exec(claims))
        replay = set(
            await session.exec(
                claims.where(*FactClaim.visible_at(as_of)).execution_options(**GATE_OFF)
            )
        )
    return live, live_expected, replay, replay_expected


@given(timeline=fact_timeline())
def test_live_gate_surfaces_only_current_and_as_of_replays_history(
    timeline: tuple[list[TemporalState], datetime],
) -> None:
    states, probe = timeline
    now = datetime.now(UTC)
    live, live_expected, replay, replay_expected = dbutil.run(read_versions(states, now, probe))
    assert live == live_expected
    assert len(live) <= 1
    assert replay == replay_expected


def test_record_access_bumps_recency_and_count_and_no_ops_on_empty() -> None:
    async def body() -> tuple[int, bool]:
        owner = await seedgraph.fresh_owner()
        async with dbutil.actor(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
            _, claim = await seedgraph.add_fact(session, owner, subject, statement="surfaced")
        async with dbutil.actor(owner) as session:
            await FactClaim.record_access(session, [])
            await FactClaim.record_access(session, [claim])
        async with dbutil.actor(owner) as session:
            row = (await session.exec(select(LiveFact).where(LiveFact.id == claim))).first()
            assert row is not None
            return row.access_count, row.last_accessed is not None

    access_count, has_recency = dbutil.run(body())
    assert access_count == 1
    assert has_recency is True


def test_skip_live_gate_reveals_the_full_history() -> None:
    async def body() -> tuple[int, int]:
        owner = await seedgraph.fresh_owner()
        now = datetime.now(UTC)
        async with dbutil.actor(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
            await seedgraph.add_fact(
                session,
                owner,
                subject,
                statement="retired",
                recorded=Range(now - timedelta(hours=1), now),
                valid=Range(None, now),
            )
            await seedgraph.add_fact(session, owner, subject, statement="current")
        base = (
            select(func.count())
            .select_from(FactClaim)
            .join(FactContent, FactContent.id == FactClaim.content_id)
            .where(FactContent.subject_id == subject)
        )
        async with dbutil.actor(owner) as session:
            live = (await session.exec(base)).one()
            history = (await session.exec(base.execution_options(**GATE_OFF))).one()
        return live or 0, history or 0

    live, history = dbutil.run(body())
    assert live == 1
    assert history == 2


def test_created_at_mirrors_the_recorded_lower_bound() -> None:
    lower = datetime(2024, 6, 1, tzinfo=UTC)
    claim = FactClaim(
        content_id=uuid.uuid4(), created_by=uuid.uuid4(), recorded=Range(lower, None)
    )
    assert claim.created_at == lower
