import uuid

import dbutil
import pytest
import seedgraph
from sqlalchemy import func, select

import aizk.graph.session_tier as session_tier_module
from aizk.config import settings
from aizk.graph.session_tier import promote_sessions
from aizk.store import Chunk, SessionItem, acting_as

pytestmark = pytest.mark.usefixtures("migrated_db", "fake_embedder")


async def noop_enqueue(*args: object, **kwargs: object) -> int:
    """Stand in for the durable enqueue so the promotion pass is read apart from the worker."""
    return 0


async def seed_item(owner: uuid.UUID, text: str, scopes: tuple[uuid.UUID, ...] = ()) -> uuid.UUID:
    """Insert one still-working session item with arbitrary scopes, bypassing the write policy.

    owner: principal that owns the working item.
    text: the remembered content promotion reingests.
    scopes: group set the item is shared with, private when empty.
    """
    item_id = uuid.uuid4()
    await dbutil.admin_exec(
        "INSERT INTO session_item (id, owner_id, scopes, kind, text) "
        "VALUES (:id, :owner, CAST(:scopes AS uuid[]), 'note', :text)",
        {"id": item_id, "owner": owner, "scopes": [str(s) for s in scopes], "text": text},
    )
    return item_id


def test_promote_moves_due_items_into_the_graph_and_skips_unwritable_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due private item reingests into a graph chunk and clears, a read-only-scope item stays.

    With the age cutoff dropped so everything is due, the promotion pass feeds the writable private
    item through the ingest pipeline into a chunk and stamps it promoted, while the item scoped to
    a group the principal only reads is filtered out by `writable_scopes` and stays working.
    """
    monkeypatch.setattr(session_tier_module, "enqueue_pending", noop_enqueue)
    monkeypatch.setattr(settings, "session_promote_age_minutes", 0.0)
    marker = uuid.uuid4().hex

    async def body() -> tuple[int, int, bool, bool]:
        owner = await seedgraph.fresh_owner()
        readonly = await dbutil.seed_group(uuid.uuid4())
        await dbutil.seed_membership(owner, readonly, "reader")
        private = await seed_item(owner, f"a decision about {marker} worth keeping")
        blocked = await seed_item(owner, f"team note {marker}", scopes=(readonly,))
        promoted = await promote_sessions(owner)
        async with acting_as(owner) as session:
            chunks = await session.scalar(
                select(func.count()).select_from(Chunk).where(Chunk.text.ilike(f"%{marker}%"))
            )
            private_item = await session.get(SessionItem, private)
            blocked_item = await session.get(SessionItem, blocked)
        assert private_item is not None and blocked_item is not None
        return (
            promoted,
            chunks or 0,
            private_item.promoted_at is not None,
            blocked_item.promoted_at is not None,
        )

    promoted, chunks, private_done, blocked_done = dbutil.run(body())
    assert promoted == 1  # only the writable private item is due
    assert chunks >= 1  # promotion reingested it into a graph chunk
    assert private_done is True  # and stamped it out of the working set
    assert blocked_done is False  # the read-only-scope item stays working


def test_promote_is_a_no_op_when_nothing_is_due(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty working set promotes nothing, so a quiet principal never touches the graph."""
    monkeypatch.setattr(session_tier_module, "enqueue_pending", noop_enqueue)

    async def body() -> int:
        owner = await seedgraph.fresh_owner()
        return await promote_sessions(owner)

    assert dbutil.run(body()) == 0


def test_promote_defaults_to_the_system_principal_on_an_empty_working_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no principal given the pass acts as the system principal over an empty working set.

    Covers the `principal_id or system` default branch, so the selection runs over nothing due and
    returns zero before any reingest.
    """
    monkeypatch.setattr(session_tier_module, "enqueue_pending", noop_enqueue)

    async def body() -> int:
        await dbutil.reset_db()
        return await promote_sessions()

    assert dbutil.run(body()) == 0
