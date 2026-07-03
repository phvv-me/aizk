import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from graphdb import owned_principal
from hypothesis import given
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range
from strategies import TemporalState, fact_timeline, temporal_states

from aizk.config import settings
from aizk.retrieval import FactHit, RecallResult, graph_search, recall
from aizk.serving.embed import Embedder
from aizk.store import EntityClaim, EntityContent, FactClaim, FactContent, LiveFact, acting_as

# a fixed unit halfvec, enough for a seeded row to embed while the temporal gate, not similarity,
# decides what a live read surfaces.
UNIT_VECTOR = [1.0] + [0.0] * (settings.embed_dim - 1)


def test_visible_at_lists_the_live_gate_for_now_and_two_windows_for_a_replay() -> None:
    """`visible_at(None)` is the single live-gate predicate, an as_of lists both window bounds.

    The live branch reuses the same `is_current` predicate every lane leans on, so it lists exactly
    one clause, while a world-time replay lists the valid-time and transaction-time containment the
    listener-opt-out path re-applies by hand.
    """
    assert len(FactClaim.visible_at(None)) == 1
    assert len(FactClaim.visible_at(datetime(2020, 1, 1, tzinfo=UTC))) == 2


@given(state=temporal_states())
def test_is_current_matches_the_temporal_spec(state: TemporalState) -> None:
    """The instance live gate counts a claim current exactly when the independent spec says it is.

    The spec hides a superseded version, a future-dated claim, and a closed-window claim, so a
    FactClaim built from each generated state agrees with `expected_current`, the gate's mirror in
    the ORM. `content_id`/`owner_id` are throwaway ids, transient and never persisted, just to
    satisfy the claim's own required columns.
    """
    now = datetime.now(UTC)
    claim = FactClaim(
        content_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        valid=state.valid(now),
        recorded=state.recorded(now),
    )
    assert claim.is_current is state.expected_current(now)


async def live_versions(states: list[TemporalState], now: datetime) -> tuple[set[str], set[str]]:
    """Seed one statement's version history and read back the live and the latest-open statements.

    Returns the statements a live, gated `select(LiveFact)` surfaces and, independently, the ones
    the spec marks current, so the property can assert the database gate never widens past the
    spec.

    states: the version history, exactly one of which is the latest.
    now: the reference instant the windows are measured around.
    """
    async with owned_principal() as owner:
        subject = uuid.uuid4()
        async with acting_as(owner) as session:
            session.add(EntityContent(id=subject, name="Subject", type="Concept", embedding=None))
            await session.flush()
            session.add(EntityClaim(content_id=subject, owner_id=owner))
            await session.flush()
            expected: set[str] = set()
            for index, state in enumerate(states):
                statement = f"version {index}"
                if state.expected_current(now):
                    expected.add(statement)
                content = uuid.uuid4()
                session.add(
                    FactContent(
                        id=content,
                        subject_id=subject,
                        predicate="related_to",
                        statement=statement,
                        embedding=UNIT_VECTOR,
                    )
                )
                await session.flush()
                session.add(
                    FactClaim(
                        content_id=content,
                        owner_id=owner,
                        valid=state.valid(now),
                        recorded=state.recorded(now),
                    )
                )
        async with acting_as(owner) as session:
            live = {fact.statement for fact in await session.scalars(select(LiveFact))}
        return live, expected


@pytest.mark.usefixtures("migrated_db")
@given(timeline=fact_timeline())
def test_live_gate_never_surfaces_a_non_current_version(
    timeline: tuple[list[TemporalState], datetime],
) -> None:
    """A gated live read surfaces only the version the spec calls current, never a retired one.

    With at most one latest version and a window that may be open, closed, or future, the database
    live gate returns exactly the statements `expected_current` marks, so a superseded, closed, or
    future-dated version is never read as current even sharing the subject.
    """
    states, _ = timeline
    now = datetime.now(UTC)
    live, expected = asyncio.run(live_versions(states, now))
    assert live == expected
    assert len(live) <= 1


async def supersede_probe() -> tuple[set[str], set[str], str, str]:
    """Plant a superseded and a current version, then read now and replay before the transition.

    The superseded claim carries the query's own embedding so it is the closest cosine match, yet a
    correct read path hides it on the live read because its `recorded` range already closed, and an
    as_of replay before the switch surfaces it while hiding the not-yet-born current one.
    """
    marker = uuid.uuid4().hex
    query = f"where does the subject live {marker}"
    superseded = f"old city {marker}"
    current = f"new city {marker}"
    [query_vector] = await Embedder().embed([query], mode="query")
    far_vector = [0.0] * (settings.embed_dim - 1) + [1.0]

    now = datetime.now(UTC)
    born = now - timedelta(hours=1)
    switch = now - timedelta(minutes=30)
    midpoint = now - timedelta(minutes=45)

    async with owned_principal() as owner:
        subject = uuid.uuid4()
        async with acting_as(owner) as session:
            session.add(
                EntityContent(
                    id=subject, name=f"Subj {marker}", type="Author", embedding=query_vector
                )
            )
            await session.flush()
            session.add(EntityClaim(content_id=subject, owner_id=owner))
            await session.flush()
            superseded_content = uuid.uuid4()
            session.add(
                FactContent(
                    id=superseded_content,
                    subject_id=subject,
                    predicate="related_to",
                    statement=superseded,
                    embedding=query_vector,
                )
            )
            await session.flush()
            session.add(
                FactClaim(
                    content_id=superseded_content,
                    owner_id=owner,
                    valid=Range(born, switch),
                    recorded=Range(born, switch),
                )
            )
            current_content = uuid.uuid4()
            session.add(
                FactContent(
                    id=current_content,
                    subject_id=subject,
                    predicate="related_to",
                    statement=current,
                    embedding=far_vector,
                )
            )
            await session.flush()
            session.add(
                FactClaim(
                    content_id=current_content,
                    owner_id=owner,
                    valid=Range(switch, None),
                    recorded=Range(switch, None),
                )
            )
        live = await graph_search(query, k=10, principal_id=owner)
        past = await graph_search(query, k=10, principal_id=owner, as_of=midpoint)
        return (
            {hit.statement for hit in live},
            {hit.statement for hit in past},
            superseded,
            current,
        )


@pytest.mark.usefixtures("migrated_db", "fake_embedder", "fake_settings")
def test_recall_hides_superseded_now_and_replays_it_in_the_past() -> None:
    """A live read surfaces only the current fact, while an as_of replay before the switch reverses
    that, surfacing the retired version and hiding the not-yet-born current one."""
    now_statements, past_statements, superseded, current = asyncio.run(supersede_probe())

    assert current in now_statements
    assert superseded not in now_statements
    assert superseded in past_statements
    assert current not in past_statements


async def replay_recall(seed: bool) -> RecallResult:
    """Recall under a fresh principal at a past as_of, over one seeded claim or an empty graph.

    Drives the full `Recall.assemble_context` replay branch, the as_of-aware seed and neighbor
    lanes the live default never reaches, so a populated graph exercises the neighbor query and an
    empty one exercises its no-seed short-circuit.

    seed: whether one subject entity content and a claim valid at the replay instant are planted
        first.
    """
    now = datetime.now(UTC)
    midpoint = now - timedelta(minutes=30)
    async with owned_principal() as owner:
        if seed:
            subject = uuid.uuid4()
            async with acting_as(owner) as session:
                session.add(EntityContent(id=subject, name="Subj", type="Concept", embedding=None))
                await session.flush()
                session.add(EntityClaim(content_id=subject, owner_id=owner))
                await session.flush()
                content = uuid.uuid4()
                session.add(
                    FactContent(
                        id=content,
                        subject_id=subject,
                        predicate="related_to",
                        statement="a past fact",
                        embedding=UNIT_VECTOR,
                    )
                )
                await session.flush()
                session.add(
                    FactClaim(
                        content_id=content,
                        owner_id=owner,
                        valid=Range(now - timedelta(hours=1), None),
                        recorded=Range(now - timedelta(hours=1), None),
                    )
                )
        return await recall("where does the subject live", principal_id=owner, as_of=midpoint)


@pytest.mark.usefixtures("migrated_db", "fake_embedder", "fake_reranker", "fake_settings")
@pytest.mark.parametrize("seed", [True, False], ids=["populated", "empty"])
def test_recall_replays_the_graph_at_as_of(seed: bool) -> None:
    """An as_of recall runs the historical seed and neighbor lanes and returns a stamped bundle."""
    result = asyncio.run(replay_recall(seed))
    assert isinstance(result, RecallResult)
    assert result.as_of is not None
    assert all(isinstance(fact, FactHit) for fact in result.facts)


@pytest.mark.usefixtures("migrated_db")
def test_skip_live_gate_reveals_the_full_history() -> None:
    """Opting out of the live gate counts both versions, the raw-history read promotion uses."""

    async def probe() -> int:
        async with owned_principal() as owner:
            subject = uuid.uuid4()
            now = datetime.now(UTC)
            async with acting_as(owner) as session:
                session.add(EntityContent(id=subject, name="Subj", type="Concept", embedding=None))
                await session.flush()
                session.add(EntityClaim(content_id=subject, owner_id=owner))
                await session.flush()
                retired_content = uuid.uuid4()
                session.add(
                    FactContent(
                        id=retired_content,
                        subject_id=subject,
                        predicate="related_to",
                        statement="retired",
                        embedding=UNIT_VECTOR,
                    )
                )
                await session.flush()
                session.add(
                    FactClaim(
                        content_id=retired_content,
                        owner_id=owner,
                        valid=Range(None, now),
                        recorded=Range(now - timedelta(hours=1), now),
                    )
                )
                current_content = uuid.uuid4()
                session.add(
                    FactContent(
                        id=current_content,
                        subject_id=subject,
                        predicate="related_to",
                        statement="current",
                        embedding=UNIT_VECTOR,
                    )
                )
                await session.flush()
                session.add(FactClaim(content_id=current_content, owner_id=owner))
            async with acting_as(owner) as session:
                rows = await session.scalars(
                    select(FactClaim)
                    .join(FactContent, FactContent.id == FactClaim.content_id)
                    .where(FactContent.subject_id == subject)
                    .execution_options(**{settings.skip_live_gate: True})
                )
                return len(list(rows))

    assert asyncio.run(probe()) == 2
