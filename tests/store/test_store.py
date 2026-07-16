from datetime import UTC, datetime, timedelta

import dbutil
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from id_factory import uuid5
from pydantic import UUID5, UUID7

from aizk.store import Entity, SessionItem, Watermark

pytestmark = pytest.mark.usefixtures("migrated_db")


def test_entity_claim_all_accepts_an_empty_batch() -> None:
    async def body() -> None:
        owner = uuid5()
        async with dbutil.actor(owner) as session:
            await Entity.Claim.claim_all(session, [], owner, frozenset({owner}))

    dbutil.run(body())


def test_watermark_bump_read_and_payload_round_trip() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid5()
        key = frozenset({owner})
        async with dbutil.actor(owner) as db:
            assert await Watermark.read(db, key, Watermark.Kind.fact_count) == 0
            await Watermark.bump_many(db, key, Watermark.Kind.entity_dirty, [])
            await Watermark.bump_many(db, key, Watermark.Kind.entity_dirty, ["a", "b", "a"], by=2)
            assert await Watermark.read(db, key, Watermark.Kind.entity_dirty, "a") == 2
            assert await Watermark.read(db, key, Watermark.Kind.entity_dirty, "b") == 2
            pending = await Watermark.pending_refs(db, key, Watermark.Kind.entity_dirty, 1)
            assert pending == {"a": 2}
            await Watermark.bump(db, key, Watermark.Kind.entity_dirty, "a")
            await Watermark.consume(db, key, Watermark.Kind.entity_dirty, pending)
            await Watermark.consume(db, key, Watermark.Kind.entity_dirty, {})
            assert await Watermark.read(db, key, Watermark.Kind.entity_dirty, "a") == 1
            assert await Watermark.bump(db, key, Watermark.Kind.fact_count, by=3) == 3
            assert await Watermark.bump(db, key, Watermark.Kind.fact_count, by=2) == 5
            await Watermark.set_value(db, key, Watermark.Kind.config, counter=9, payload={"k": 1})
            assert await Watermark.read(db, key, Watermark.Kind.config) == 9
            assert await Watermark.read_payload(db, key, Watermark.Kind.config) == {"k": 1}
            assert await Watermark.read_payload(db, key, Watermark.Kind.curation_pending) == {}

    dbutil.run(body())


def test_watermark_is_private_to_its_owner() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid5()
        other = uuid5()
        async with dbutil.actor(owner) as db:
            await Watermark.bump(db, frozenset({owner}), Watermark.Kind.fact_count, by=7)
        async with dbutil.actor(other) as db:
            assert await Watermark.read(db, frozenset({owner}), Watermark.Kind.fact_count) == 0

    dbutil.run(body())


@settings(max_examples=10, deadline=None)
@given(
    ages=st.lists(st.integers(min_value=0, max_value=24 * 60), max_size=8),
    age_minutes=st.integers(min_value=1, max_value=24 * 60),
    threshold=st.integers(min_value=0, max_value=8),
)
def test_session_item_promotion_is_the_ordered_union_of_age_and_overflow(
    migrated_db: None, ages: list[int], age_minutes: int, threshold: int
) -> None:
    """The database decides due items: aged past the cutoff or the oldest overflow, oldest
    first, replayed here against the same rows."""
    owner = uuid5()
    ordered_ages = sorted(ages, reverse=True)
    overflow = max(0, len(ordered_ages) - threshold)

    async def body() -> tuple[list[UUID5 | UUID7], list[UUID5 | UUID7]]:
        await dbutil.reset_db()
        now = datetime.now(UTC)
        seeded: list[UUID5 | UUID7] = []
        for age in ordered_ages:
            item_id = uuid5()
            await dbutil.admin_exec(
                "INSERT INTO session_item (id, created_by, scopes, kind, text, created_at) "
                "VALUES (:id, :owner, CAST(:scopes AS uuid[]), 'note', 't', :created_at)",
                {
                    "id": item_id,
                    "owner": owner,
                    "scopes": [str(owner)],
                    "created_at": now - timedelta(minutes=age),
                },
            )
            seeded.append(item_id)
        expected = [
            item_id
            for index, (item_id, age) in enumerate(zip(seeded, ordered_ages, strict=True))
            # Strictly older than the cutoff: seeding time already passed since created_at.
            if age >= age_minutes or index < overflow
        ]
        async with dbutil.actor(owner) as session:
            result = await session.exec(
                SessionItem.due_for_promotion(frozenset({owner}), age_minutes, threshold)
            )
            due = [item.id for item in result.scalars()]
        return due, expected

    due, expected = dbutil.run(body())

    assert due == expected
