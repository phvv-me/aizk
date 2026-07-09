import uuid
from datetime import UTC, datetime, timedelta

import dbutil
import pytest

from aizk.store import SessionItem, Watermark, acting_as

pytestmark = pytest.mark.usefixtures("migrated_db")


def test_session_outside_a_block_fails_fast() -> None:
    """session() raises NoTenantContext when read outside any acting_as/bypass_rls block."""
    from aizk.exceptions import NoTenantContext
    from aizk.store.engine import session

    with pytest.raises(NoTenantContext):
        session()


def test_watermark_bump_read_and_payload_round_trip() -> None:
    """`bump` accumulates, `set_value` writes absolutely, and payloads read back under RLS."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        async with acting_as(owner):
            assert await Watermark.read(owner, Watermark.Kind.fact_count) == 0
            assert await Watermark.bump(owner, Watermark.Kind.fact_count, by=3) == 3
            assert await Watermark.bump(owner, Watermark.Kind.fact_count, by=2) == 5
            await Watermark.set_value(owner, Watermark.Kind.scorecard, counter=9, payload={"k": 1})
            assert await Watermark.read(owner, Watermark.Kind.scorecard) == 9
            assert await Watermark.read_payload(owner, Watermark.Kind.scorecard) == {"k": 1}
            assert await Watermark.read_payload(owner, Watermark.Kind.config) == {}

    dbutil.run(body())


def test_watermark_is_private_to_its_owner() -> None:
    """A watermark counter never leaks across users, the private-bookkeeping guarantee."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        other = uuid.uuid4()
        async with acting_as(owner):
            await Watermark.bump(owner, Watermark.Kind.fact_count, by=7)
        async with acting_as(other):
            assert await Watermark.read(owner, Watermark.Kind.fact_count) == 0

    dbutil.run(body())


def test_session_item_due_for_promotion_unions_aged_and_overflow() -> None:
    """`due_for_promotion` returns aged items plus the oldest overflow, oldest-first, deduped."""
    now = datetime(2024, 1, 10, tzinfo=UTC)

    def item(minutes_old: float, ident: uuid.UUID) -> SessionItem:
        made = SessionItem(text="t", owner_id=uuid.uuid4())
        made.id = ident
        made.created_at = now - timedelta(minutes=minutes_old)
        return made

    aged = item(120, uuid.uuid4())
    fresh_a = item(1, uuid.uuid4())
    fresh_b = item(2, uuid.uuid4())
    items = [aged, fresh_b, fresh_a]  # oldest first
    due = SessionItem.due_for_promotion(items, now, age_minutes=60, threshold=1)
    # the aged item passes the age cutoff; overflow=len-threshold=2 takes the two oldest by index
    assert aged in due
    assert [i.id for i in due] == [i.id for i in items if i in due]


def test_session_item_nothing_due_when_fresh_and_under_threshold() -> None:
    """A small, fresh working set drains nothing, the steady-state no-op."""
    now = datetime(2024, 1, 10, tzinfo=UTC)
    made = SessionItem(text="t", owner_id=uuid.uuid4())
    made.id = uuid.uuid4()
    made.created_at = now
    assert SessionItem.due_for_promotion([made], now, age_minutes=60, threshold=20) == []
