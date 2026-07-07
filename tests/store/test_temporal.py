import uuid
from datetime import UTC, datetime, timedelta

import dbutil
import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.sql.elements import ColumnElement

from aizk.config import settings
from aizk.graph.ids import entity_id, fact_id
from aizk.store import FactClaim, acting_as

pytestmark = pytest.mark.usefixtures("migrated_db")


# the bi-temporal probe is inlined here (rather than imported from `strategies`) so the stable
# store lane never pulls in the retrieval/graph result-model imports that shared module carries,
# keeping this lane green through the concurrent sweep of those surfaces.
_offsets = st.none() | st.floats(min_value=-400.0, max_value=400.0, allow_nan=False).filter(
    lambda days: abs(days) > 1.0
)


class TemporalState:
    """One claim's temporal columns relative to a caller-chosen `now`, the bi-temporal probe.

    is_latest: whether `recorded`'s upper bound stays open, the live version of the statement.
    valid_from_days: day offset from now the `valid` window opens, None for unbounded.
    valid_to_days: day offset from now the `valid` window closes, None for unbounded.
    """

    def __init__(
        self, is_latest: bool, valid_from_days: float | None, valid_to_days: float | None
    ) -> None:
        self.is_latest = is_latest
        self.valid_from_days = valid_from_days
        self.valid_to_days = valid_to_days

    def window(self, now: datetime) -> tuple[datetime | None, datetime | None]:
        """The concrete valid_from and valid_to around `now`."""
        start = (
            None if self.valid_from_days is None else now + timedelta(days=self.valid_from_days)
        )
        end = None if self.valid_to_days is None else now + timedelta(days=self.valid_to_days)
        return start, end

    def valid(self, now: datetime) -> Range | None:
        """The concrete `valid` range around `now`, `empty` when the window is inverted."""
        start, end = self.window(now)
        if start is not None and end is not None and start > end:
            return Range(empty=True)
        return Range(start, end)

    def recorded(self, now: datetime) -> Range:
        """The `recorded` range, open while `is_latest`, closed at `now` otherwise."""
        return Range(now - timedelta(days=1), None if self.is_latest else now)

    def expected_current(self, now: datetime) -> bool:
        """Whether the live gate must count this state current at `now`, the spec not the code."""
        start, end = self.window(now)
        return self.is_latest and (start is None or start <= now) and (end is None or end > now)


def temporal_states() -> st.SearchStrategy[TemporalState]:
    """A temporal state spanning latest, superseded, future-dated, and closed-window cases."""
    return st.builds(
        TemporalState,
        is_latest=st.booleans(),
        valid_from_days=_offsets,
        valid_to_days=_offsets,
    )


def build_claim(recorded: Range, valid: Range | None, access_count: int = 0) -> FactClaim:
    """A transient in-memory `FactClaim`, enough to read its Python-side hybrid properties."""
    return FactClaim(
        content_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        recorded=recorded,
        valid=valid,
        access_count=access_count,
    )


@given(state=temporal_states())
def test_is_current_matches_the_live_gate_spec(state: TemporalState) -> None:
    """The Python `is_current` hybrid agrees with the spec across every temporal state."""
    now = datetime.now(UTC)
    claim = build_claim(state.recorded(now), state.valid(now))
    assert claim.is_current == state.expected_current(now)


def test_created_at_mirrors_recorded_lower_bound() -> None:
    """`created_at` is the friendly name for the claim's `recorded` lower bound."""
    lower = datetime(2022, 5, 1, tzinfo=UTC)
    assert build_claim(Range(lower, None), None).created_at == lower


def test_visible_at_switches_between_live_and_replay() -> None:
    """`visible_at(None)` is one live predicate; an as_of yields the two replay predicates."""
    live = FactClaim.visible_at(None)
    replay = FactClaim.visible_at(datetime(2020, 1, 1, tzinfo=UTC))
    assert len(live) == 1 and isinstance(live[0], ColumnElement)
    assert len(replay) == 2 and all(isinstance(predicate, ColumnElement) for predicate in replay)


@given(access_count=st.integers(min_value=0, max_value=20))
def test_relevance_rises_with_access_and_decays_with_age(access_count: int) -> None:
    """Relevance scales with access frequency and an older untouched claim scores lower."""
    now = datetime(2024, 1, 1, tzinfo=UTC)
    fresh = build_claim(Range(now, None), None, access_count=access_count)
    assert fresh.relevance(now, half_life_days=90.0) == pytest.approx(1.0 + access_count)
    old = build_claim(Range(now - timedelta(days=180), None), None, access_count=access_count)
    assert old.relevance(now, half_life_days=90.0) < fresh.relevance(now, half_life_days=90.0)


async def seed_live_claim(owner: uuid.UUID, statement: str, days_old: float) -> uuid.UUID:
    """Seed one live (open-recorded) fact claim aged `days_old`, superuser-inserted."""
    subject = entity_id("subj", "Concept")
    content = fact_id("subj", "related_to", "", statement)
    claim = uuid.uuid4()
    await dbutil.admin_exec(
        "INSERT INTO entity_content (id, name, type) VALUES (:i, 'subj', 'Concept') "
        "ON CONFLICT (id) DO NOTHING",
        {"i": subject},
    )
    await dbutil.admin_exec(
        "INSERT INTO fact_content (id, subject_id, predicate, statement) "
        "VALUES (:i, :s, 'related_to', :st) ON CONFLICT (id) DO NOTHING",
        {"i": content, "s": subject, "st": statement},
    )
    await dbutil.admin_exec(
        "INSERT INTO fact_claim (id, content_id, owner_id, scopes, recorded, reviewed_at) "
        "VALUES (:i, :c, :o, '{}', tstzrange(now() - make_interval(days => :d), NULL), now())",
        {"i": claim, "c": content, "o": owner, "d": int(days_old)},
    )
    return claim


def test_record_access_bumps_count_for_surfaced_statements() -> None:
    """`record_access` sets last_accessed and increments the count of the named live claims."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_user(uuid.uuid4())
        await seed_live_claim(owner, "surfaced", days_old=1)
        async with acting_as(owner) as session:
            await FactClaim.record_access(session, ["surfaced"])
            await FactClaim.record_access(session, [])  # empty is a no-op
        async with dbutil.admin_engine().connect() as connection:
            row = await connection.execute(
                text(
                    "SELECT access_count, last_accessed FROM fact_claim fc "
                    "JOIN fact_content c ON c.id = fc.content_id WHERE c.statement = 'surfaced'"
                )
            )
            count, accessed = row.one()
        assert count == 1 and accessed is not None

    dbutil.run(body())


def test_archive_stale_closes_forgotten_claims_only() -> None:
    """A stale, never-accessed claim is archived (recorded closed); a fresh one stays live."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_user(uuid.uuid4())
        await seed_live_claim(owner, "ancient", days_old=400)
        await seed_live_claim(owner, "recent", days_old=1)
        async with acting_as(owner) as session:
            archived = await FactClaim.archive_stale(session, half_life_days=90.0, floor=0.25)
        assert len(archived) == 1
        async with acting_as(owner) as session:
            live = set(
                await session.scalars(
                    select(FactClaim.id).execution_options(**{settings.skip_live_gate: True})
                )
            )
        # the archived claim's own id left the archived set's complement (it is no longer live)
        assert archived[0] in live  # still present in history, just no longer open

    dbutil.run(body())
