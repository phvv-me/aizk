import uuid
from datetime import UTC, datetime, timedelta

import dbutil
import pytest
import seedgraph
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import Range

from aizk.config import settings
from aizk.graph.decay import decay
from aizk.store import FactClaim, acting_as

pytestmark = pytest.mark.usefixtures("migrated_db")

# finite ages, counts, and half-lives the decay score is defined over, kept off the extremes that
# would make the floating-point comparison itself the thing under test rather than monotonicity.
ages = st.floats(min_value=0.0, max_value=3650.0, allow_nan=False, allow_infinity=False)
counts = st.integers(min_value=0, max_value=1000)
half_lives = st.floats(min_value=1.0, max_value=365.0, allow_nan=False, allow_infinity=False)


def aged_claim(now: datetime, age_days: float, access_count: int) -> FactClaim:
    """A transient latest claim last accessed `age_days` before `now` with `access_count` recalls.

    Never persisted, so the ids are throwaway; `relevance` reads only `last_accessed`, `recorded`,
    and `access_count`.

    now: reference instant the age is measured back from.
    age_days: days since the claim was last reached for.
    access_count: how often recall has surfaced it.
    """
    return FactClaim(
        content_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        last_accessed=now - timedelta(days=age_days),
        recorded=Range(now - timedelta(days=age_days), None),
        access_count=access_count,
    )


@given(age=ages, low=counts, high=counts, half_life=half_lives)
def test_relevance_never_falls_as_access_count_rises(
    age: float, low: int, high: int, half_life: float
) -> None:
    """At a fixed age more recalls never lower relevance, the frequency lift the floor reads."""
    now = datetime.now(UTC)
    lo, hi = sorted((low, high))
    quiet = aged_claim(now, age, lo).relevance(now, half_life)
    busy = aged_claim(now, age, hi).relevance(now, half_life)
    assert busy >= quiet


@given(young=ages, old=ages, count=counts, half_life=half_lives)
def test_relevance_never_rises_with_age(
    young: float, old: float, count: int, half_life: float
) -> None:
    """At a fixed access count an older claim never outscores a fresher one, the recency decay."""
    now = datetime.now(UTC)
    near, far = sorted((young, old))
    fresh = aged_claim(now, near, count).relevance(now, half_life)
    stale = aged_claim(now, far, count).relevance(now, half_life)
    assert fresh >= stale


def test_relevance_brackets_the_decay_floor() -> None:
    """A decade-untouched claim decays under the floor while a just-written one clears it."""
    now = datetime.now(UTC)
    assert aged_claim(now, 3650.0, 0).relevance(now, 90.0) < settings.decay_floor
    assert aged_claim(now, 0.0, 0).relevance(now, 90.0) >= settings.decay_floor


async def claim_state(owner: uuid.UUID, claim: uuid.UUID) -> tuple[bool, bool]:
    """The (still-live, decay-marked) state of one claim after a pass, read back as its owner.

    owner: user whose visibility scopes the read.
    claim: the claim to inspect.
    """
    async with acting_as(owner) as session:
        row = await session.execute(
            text(
                "SELECT upper_inf(recorded), attributes::text ILIKE '%decay%' "
                "FROM fact_claim WHERE id = :id"
            ),
            {"id": claim},
        )
        is_latest, decayed = row.one()
    return bool(is_latest), bool(decayed)


async def plant_aged(
    owner: uuid.UUID,
    subject: uuid.UUID,
    statement: str,
    age_days: float,
    count: int,
    accessed: bool,
) -> uuid.UUID:
    """Plant one latest claim with a chosen age and access history, return its claim id.

    owner: user that owns the claim.
    subject: entity content the fact hangs from.
    statement: self-contained text.
    age_days: days in the past the claim entered memory.
    count: how many times recall has surfaced it.
    accessed: whether the claim carries a recent last_accessed.
    """
    now = datetime.now(UTC)
    async with acting_as(owner) as session:
        content = uuid.uuid4()
        session.add(
            seedgraph.FactContent(
                id=content, subject_id=subject, predicate="related_to", statement=statement
            )
        )
        await session.flush()
        claim = FactClaim(
            content_id=content,
            owner_id=owner,
            recorded=Range(now - timedelta(days=age_days), None),
            last_accessed=now if accessed else None,
            access_count=count,
        )
        session.add(claim)
        await session.flush()
        return claim.id


def test_decay_archives_the_stale_claim_and_keeps_the_fresh_one() -> None:
    """Decay closes `recorded` and marks the stale claim decayed while the busy fresh one stays.

    The set-based `DECAY_SQL`/`archive_stale` scores each visible latest claim, archives the one
    under the floor by closing its transaction time and stamping `decayed`, and returns its id, so
    a decade-old untouched claim is retired while a just-written, often-recalled one is untouched.
    """

    async def body() -> tuple[int, tuple[bool, bool], tuple[bool, bool]]:
        owner = await seedgraph.fresh_owner()
        async with acting_as(owner) as session:
            subject = await seedgraph.add_entity(session, owner, "Subject")
        stale = await plant_aged(owner, subject, "stale", 3650.0, 0, accessed=False)
        fresh = await plant_aged(owner, subject, "fresh", 0.0, 50, accessed=True)
        count = await decay(user_id=owner, half_life_days=90.0)
        return count, await claim_state(owner, stale), await claim_state(owner, fresh)

    count, (stale_live, stale_marked), (fresh_live, _) = dbutil.run(body())
    assert count == 1
    assert stale_live is False and stale_marked is True
    assert fresh_live is True


def test_decay_defaults_to_the_system_user_and_archives_nothing_on_empty() -> None:
    """With no user given the pass acts as the system user and archives an empty graph.

    Covers the `user_id or system` default branch without seeding a claim, so the archival
    runs over nothing and reports zero.
    """

    async def body() -> int:
        await dbutil.reset_db()
        return await decay()

    assert dbutil.run(body()) == 0
