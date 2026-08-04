from datetime import UTC, datetime, timedelta

import dbutil
import pytest
import seedgraph
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5
from pydantic import UUID5, UUID7
from sqlalchemy import text

from aizk.config import settings
from aizk.graph.decay import decay
from aizk.store import Fact

pytestmark = pytest.mark.usefixtures("migrated_db")

# Stay away from floating-point extremes while testing monotonicity.
ages = st.floats(min_value=0.0, max_value=3650.0, allow_nan=False, allow_infinity=False)
counts = st.integers(min_value=0, max_value=1000)
half_lives = st.floats(min_value=1.0, max_value=365.0, allow_nan=False, allow_infinity=False)


def aged_claim(now: datetime, age_days: float, access_count: int) -> Fact.Claim:
    return Fact.Claim(
        content_id=uuid5(),
        created_by=uuid5(),
        last_accessed=now - timedelta(days=age_days),
        recorded_from=now - timedelta(days=age_days),
        access_count=access_count,
    )


@given(young=ages, old=ages, low=counts, high=counts, half_life=half_lives)
def test_relevance_is_bounded_and_monotonic_in_age_and_access(
    young: float,
    old: float,
    low: int,
    high: int,
    half_life: float,
) -> None:
    now = datetime.now(UTC)
    near, far = sorted((young, old))
    quiet_count, busy_count = sorted((low, high))
    busy = aged_claim(now, near, busy_count).relevance(now, half_life)
    quiet_score = aged_claim(now, near, quiet_count).relevance(now, half_life)
    stale = aged_claim(now, far, quiet_count).relevance(now, half_life)
    assert quiet_score >= stale
    assert busy >= quiet_score
    assert aged_claim(now, 0.0, quiet_count).relevance(now, half_life) == pytest.approx(
        1.0 + quiet_count
    )
    assert aged_claim(now, 3650.0, 0).relevance(now, 90.0) < settings.decay_floor
    assert aged_claim(now, 0.0, 0).relevance(now, 90.0) >= settings.decay_floor


async def claim_state(owner: UUID5 | UUID7, claim: UUID5 | UUID7) -> tuple[bool, bool]:
    async with dbutil.actor(owner) as session:
        row = await session.exec(
            text(
                "SELECT recorded_to IS NULL, attributes::text ILIKE '%decay%' "
                "FROM fact_claim WHERE id = :id"
            ),
            params={"id": claim},
        )
        is_latest, decayed = row.one()
    return bool(is_latest), bool(decayed)


async def plant_aged(
    owner: UUID5 | UUID7,
    subject: UUID5 | UUID7,
    statement: str,
    age_days: float,
    count: int,
    accessed: bool,
) -> UUID5 | UUID7:
    now = datetime.now(UTC)
    async with dbutil.actor(owner) as session:
        content = uuid5()
        session.add(
            seedgraph.Fact.Content(
                id=content, subject_id=subject, predicate="related_to", statement=statement
            )
        )
        await session.flush()
        claim = Fact.Claim(
            content_id=content,
            created_by=owner,
            scopes=[owner],
            recorded_from=now - timedelta(days=age_days),
            last_accessed=now if accessed else None,
            access_count=count,
        )
        session.add(claim)
        await session.flush()
        return claim.id


def test_decay_archives_only_stale_claims_and_handles_an_empty_default_scope() -> None:
    async def body() -> tuple[int, tuple[bool, bool], tuple[bool, bool], int]:
        owner = await seedgraph.fresh_owner()
        async with dbutil.actor(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
        stale = await plant_aged(owner, subject, "stale", 3650.0, 0, accessed=False)
        fresh = await plant_aged(owner, subject, "fresh", 0.0, 50, accessed=True)
        count = await decay(scopes=frozenset({owner}), half_life_days=90.0)
        stale_state = await claim_state(owner, stale)
        fresh_state = await claim_state(owner, fresh)
        await dbutil.reset_db()
        return count, stale_state, fresh_state, await decay()

    count, (stale_live, stale_marked), (fresh_live, _), empty = dbutil.run(body())
    assert count == 1
    assert stale_live is False and stale_marked is True
    assert fresh_live is True
    assert empty == 0
