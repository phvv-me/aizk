from datetime import UTC, datetime, timedelta

import dbutil
import pytest
import seedgraph
from hypothesis import given
from hypothesis import strategies as st
from id_factory import uuid5
from pydantic import UUID5, UUID7
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import Range

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
        recorded=Range(now - timedelta(days=age_days), None),
        access_count=access_count,
    )


@given(age=ages, low=counts, high=counts, half_life=half_lives)
def test_relevance_never_falls_as_access_count_rises(
    age: float, low: int, high: int, half_life: float
) -> None:
    now = datetime.now(UTC)
    lo, hi = sorted((low, high))
    quiet = aged_claim(now, age, lo).relevance(now, half_life)
    busy = aged_claim(now, age, hi).relevance(now, half_life)
    assert busy >= quiet
    assert aged_claim(now, 0.0, lo).relevance(now, half_life) == pytest.approx(1.0 + lo)


@given(young=ages, old=ages, count=counts, half_life=half_lives)
def test_relevance_never_rises_with_age(
    young: float, old: float, count: int, half_life: float
) -> None:
    now = datetime.now(UTC)
    near, far = sorted((young, old))
    fresh = aged_claim(now, near, count).relevance(now, half_life)
    stale = aged_claim(now, far, count).relevance(now, half_life)
    assert fresh >= stale


def test_relevance_brackets_the_decay_floor() -> None:
    now = datetime.now(UTC)
    assert aged_claim(now, 3650.0, 0).relevance(now, 90.0) < settings.decay_floor
    assert aged_claim(now, 0.0, 0).relevance(now, 90.0) >= settings.decay_floor


async def claim_state(owner: UUID5 | UUID7, claim: UUID5 | UUID7) -> tuple[bool, bool]:
    async with dbutil.actor(owner) as session:
        row = await session.exec(
            text(
                "SELECT upper_inf(recorded), attributes::text ILIKE '%decay%' "
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
            recorded=Range(now - timedelta(days=age_days), None),
            last_accessed=now if accessed else None,
            access_count=count,
        )
        session.add(claim)
        await session.flush()
        return claim.id


def test_decay_archives_the_stale_claim_and_keeps_the_fresh_one() -> None:
    async def body() -> tuple[int, tuple[bool, bool], tuple[bool, bool]]:
        owner = await seedgraph.fresh_owner()
        async with dbutil.actor(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
        stale = await plant_aged(owner, subject, "stale", 3650.0, 0, accessed=False)
        fresh = await plant_aged(owner, subject, "fresh", 0.0, 50, accessed=True)
        count = await decay(scopes=frozenset({owner}), half_life_days=90.0)
        return count, await claim_state(owner, stale), await claim_state(owner, fresh)

    count, (stale_live, stale_marked), (fresh_live, _) = dbutil.run(body())
    assert count == 1
    assert stale_live is False and stale_marked is True
    assert fresh_live is True


def test_decay_defaults_to_the_system_user_and_archives_nothing_on_empty() -> None:
    async def body() -> int:
        await dbutil.reset_db()
        return await decay()

    assert dbutil.run(body()) == 0
