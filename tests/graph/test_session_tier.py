import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from graphdb import FakeLLM
from sqlalchemy import func, select

import aizk.graph.session_tier as session_tier_module
from aizk.config import settings
from aizk.extract.ingest import remember_session
from aizk.graph.session_tier import promote_sessions
from aizk.retrieval import recall
from aizk.store import Chunk, SessionItem, acting_as


def items_at(ages_minutes: list[float], now: datetime) -> list[SessionItem]:
    """Working items aged the given minutes, oldest first, the pure selector's input.

    Built in memory without a session since the selector reads only `id` and `created_at`.

    ages_minutes: age in minutes of each item, laid out oldest first.
    now: the reference the ages are subtracted from.
    """
    return [
        SessionItem(id=uuid.uuid4(), created_at=now - timedelta(minutes=age))
        for age in ages_minutes
    ]


def test_due_for_promotion_takes_the_aged_items() -> None:
    """An item past the age cutoff is due while a fresh one under a slack cap is not."""
    now = datetime.now(UTC)
    items = items_at([120.0, 5.0], now)
    due = SessionItem.due_for_promotion(items, now, age_minutes=60.0, threshold=100)
    assert [item.id for item in due] == [items[0].id]  # only the aged item, the fresh one stays


def test_due_for_promotion_overflows_the_oldest_beyond_the_cap() -> None:
    """With every item fresh, the oldest beyond the working cap are still due, staying bounded."""
    now = datetime.now(UTC)
    items = items_at([3.0, 2.0, 1.0], now)
    due = SessionItem.due_for_promotion(items, now, age_minutes=999.0, threshold=1)
    assert [item.id for item in due] == [items[0].id, items[1].id]  # oldest two overflow the cap


@pytest.mark.usefixtures("fake_embedder")
def test_promotion_moves_a_working_item_into_the_graph_and_recall_follows_it(
    fresh_principal: uuid.UUID,
    fake_llm: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remembered item recalls from the working lane, then promotion moves it into the graph.

    The single most universal 2026 tier end to end: a remember lands in working memory a recall
    surfaces at once, and the promotion pass, with the age cutoff dropped so the item is due, feeds
    it through the ingest pipeline into a document and clears it from the working lane, so the next
    recall reads it from the graph store instead. The queue enqueue is stubbed so the pass is read
    in isolation from the durable worker.
    """
    owner = fresh_principal
    marker = uuid.uuid4().hex
    text_body = f"a decision about {marker} worth keeping"

    async def probe() -> tuple[bool, int, int, bool]:
        await remember_session(text_body, kind="note", owner_id=owner)
        # the reranker seam is off so recall rides the fake embedder alone without loading a model
        monkeypatch.setattr(settings, "rerank", False)
        before = await recall(text_body, principal_id=owner, k=4)
        seen_working = any(marker in note.text for note in before.session)

        monkeypatch.setattr(session_tier_module, "enqueue_pending", _noop)
        monkeypatch.setattr(settings, "session_promote_age_minutes", 0.0)
        promoted = await promote_sessions(owner)

        after = await recall(text_body, principal_id=owner, k=4)
        still_working = any(marker in note.text for note in after.session)
        async with acting_as(owner) as session:
            chunks = await session.scalar(
                select(func.count()).select_from(Chunk).where(Chunk.text.ilike(f"%{marker}%"))
            )
        return seen_working, promoted, chunks or 0, still_working

    seen_working, promoted, chunks, still_working = asyncio.run(probe())
    assert seen_working  # recall surfaced the fresh working item
    assert promoted == 1  # the aged item was promoted
    assert chunks == 1  # promotion created the graph chunk in the store
    assert not still_working  # and cleared it from the working lane


@pytest.mark.usefixtures("fake_embedder")
def test_promotion_is_a_no_op_when_nothing_is_due(
    fresh_principal: uuid.UUID,
    fake_llm: FakeLLM,
) -> None:
    """An empty working set promotes nothing, so a quiet principal never touches the graph."""
    owner = fresh_principal
    assert asyncio.run(promote_sessions(owner)) == 0


async def _noop(*args: object, **kwargs: object) -> None:
    """An async no-op standing in for the durable enqueue during the isolated promotion test."""
