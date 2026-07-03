import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from graphdb import owned_principal
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import Range

from aizk.config import settings
from aizk.graph.decay import decay
from aizk.store import (
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    LiveFact,
    acting_as,
)

# finite ages and counts the decay score is defined over, kept away from the extremes that would
# make the floating-point comparison itself the thing under test rather than the monotonicity.
ages = st.floats(min_value=0.0, max_value=3650.0, allow_nan=False, allow_infinity=False)
counts = st.integers(min_value=0, max_value=1000)
half_lives = st.floats(min_value=1.0, max_value=365.0, allow_nan=False, allow_infinity=False)


def aged_fact(now: datetime, age_days: float, access_count: int) -> FactClaim:
    """A latest claim last accessed `age_days` before `now` with `access_count` recalls, the
    inputs.

    Transient, never persisted, so `content_id`/`owner_id` are throwaway ids just to satisfy the
    claim's own required columns; `relevance` reads only `last_accessed`, `recorded`, and
    `access_count`.

    now: the reference instant the age is measured back from, shared with the score's `now`.
    age_days: days since the claim was last reached for, the recency term.
    access_count: how often recall has surfaced it, the frequency lift.
    """
    return FactClaim(
        content_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        last_accessed=now - timedelta(days=age_days),
        recorded=Range(now - timedelta(days=age_days), None),
        access_count=access_count,
    )


@given(age=ages, low=counts, high=counts, half_life=half_lives)
def test_relevance_rises_with_access_count(
    age: float, low: int, high: int, half_life: float
) -> None:
    """At a fixed age more recalls never lower relevance, the frequency lift the floor reads."""
    now = datetime.now(UTC)
    lo, hi = sorted((low, high))
    quiet = aged_fact(now, age, lo).relevance(now, half_life)
    busy = aged_fact(now, age, hi).relevance(now, half_life)
    assert busy >= quiet


@given(young=ages, old=ages, count=counts, half_life=half_lives)
def test_relevance_decays_with_age(young: float, old: float, count: int, half_life: float) -> None:
    """At a fixed access count an older claim never outscores a fresher one, the recency decay."""
    now = datetime.now(UTC)
    near, far = sorted((young, old))
    fresh = aged_fact(now, near, count).relevance(now, half_life)
    stale = aged_fact(now, far, count).relevance(now, half_life)
    assert fresh >= stale


def test_an_untouched_ancient_fact_falls_below_the_floor() -> None:
    """A claim recall never reached for in a decade decays under the floor decay archives by."""
    now = datetime.now(UTC)
    assert aged_fact(now, 3650.0, 0).relevance(now, 90.0) < settings.decay_floor
    assert aged_fact(now, 0.0, 0).relevance(now, 90.0) >= settings.decay_floor


async def plant_fact(
    owner: uuid.UUID,
    subject: uuid.UUID,
    statement: str,
    age_days: float,
    access_count: int,
    accessed: bool,
) -> uuid.UUID:
    """Plant one latest content+claim pair with a chosen age and access history, return the claim's
    own id.

    owner: principal that owns the claim.
    subject: the entity content the fact hangs from.
    statement: self-contained text, the key record_access matches on.
    age_days: days in the past the claim entered memory.
    access_count: how many times recall has surfaced it.
    accessed: whether the claim carries a recent last_accessed.
    """
    now = datetime.now(UTC)
    content = uuid.uuid4()
    claim = uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(
            FactContent(
                id=content,
                subject_id=subject,
                predicate="related_to",
                statement=statement,
                embedding=None,
            )
        )
        await session.flush()
        session.add(
            FactClaim(
                id=claim,
                content_id=content,
                owner_id=owner,
                recorded=Range(now - timedelta(days=age_days), None),
                last_accessed=now if accessed else None,
                access_count=access_count,
            )
        )
    return claim


async def fact_state(owner: uuid.UUID, claim: uuid.UUID) -> tuple[bool, bool]:
    """The live and decay-marked state of one claim after a pass, read back as its owner.

    owner: principal whose visibility scopes the read.
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


@pytest.mark.usefixtures("migrated_db")
def test_decay_archives_the_stale_fact_and_keeps_the_fresh_one() -> None:
    """Decay closes `recorded` and marks the stale claim while the fresh, often-recalled stays."""

    async def probe() -> tuple[int, tuple[bool, bool], tuple[bool, bool]]:
        async with owned_principal() as owner:
            subject = uuid.uuid4()
            async with acting_as(owner) as session:
                session.add(EntityContent(id=subject, name="e", type="Concept", embedding=None))
                await session.flush()
                session.add(EntityClaim(content_id=subject, owner_id=owner))
            stale = await plant_fact(owner, subject, "stale", 3650.0, 0, accessed=False)
            fresh = await plant_fact(owner, subject, "fresh", 0.0, 50, accessed=True)
            count = await decay(principal_id=owner, half_life_days=90.0)
            return (
                count,
                await fact_state(owner, stale),
                await fact_state(owner, fresh),
            )

    count, (stale_latest, stale_marked), (fresh_latest, _) = asyncio.run(probe())
    assert count >= 1
    assert stale_latest is False and stale_marked is True
    assert fresh_latest is True


@pytest.mark.usefixtures("migrated_db")
def test_record_access_bumps_recency_and_count() -> None:
    """Surfacing a statement stamps last_accessed and increments its count, the decay recency lift.

    An empty surfaced set is a no-op, so the recall path can call it unconditionally without a
    needless write.
    """

    async def probe() -> tuple[int, bool]:
        async with owned_principal() as owner:
            subject = uuid.uuid4()
            async with acting_as(owner) as session:
                session.add(EntityContent(id=subject, name="e", type="Concept", embedding=None))
                await session.flush()
                session.add(EntityClaim(content_id=subject, owner_id=owner))
            claim = await plant_fact(owner, subject, "surfaced", 1.0, 0, accessed=False)
            async with acting_as(owner) as session:
                await FactClaim.record_access(session, [])
                await FactClaim.record_access(session, ["surfaced"])
            async with acting_as(owner) as session:
                row = await session.scalar(select(LiveFact).where(LiveFact.id == claim))
                assert row is not None
                return row.access_count, row.last_accessed is not None

    access_count, has_recency = asyncio.run(probe())
    assert access_count == 1
    assert has_recency is True
