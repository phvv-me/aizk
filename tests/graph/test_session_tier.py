import uuid

import dbutil
import pytest
import seedgraph
from sqlalchemy import func
from sqlmodel import select

import aizk.graph.session_tier as session_tier_module
from aizk.config import settings
from aizk.graph.session_tier import promote_sessions
from aizk.store import Chunk, SessionItem

pytestmark = pytest.mark.usefixtures("migrated_db", "fake_embedder")


async def noop_enqueue(*args: object, **kwargs: object) -> int:
    return 0


async def seed_item(owner: uuid.UUID, text: str, scopes: tuple[uuid.UUID, ...] = ()) -> uuid.UUID:
    item_id = uuid.uuid4()
    scopes = scopes or (owner,)
    await dbutil.admin_exec(
        "INSERT INTO session_item (id, created_by, scopes, kind, text) "
        "VALUES (:id, :owner, CAST(:scopes AS uuid[]), 'note', :text)",
        {"id": item_id, "owner": owner, "scopes": [str(s) for s in scopes], "text": text},
    )
    return item_id


def test_promote_moves_due_items_into_the_graph_and_skips_unwritable_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_tier_module, "enqueue_pending", noop_enqueue)
    monkeypatch.setattr(settings, "session_promote_age_minutes", 0.0)
    marker = uuid.uuid4().hex

    async def body() -> tuple[int, int, bool, bool]:
        owner = await seedgraph.fresh_owner()
        # The background caller can read this scope but cannot promote into it.
        readonly = uuid.uuid4()
        private = await seed_item(owner, f"a decision about {marker} worth keeping")
        blocked = await seed_item(owner, f"team note {marker}", scopes=(readonly,))
        promoted = await promote_sessions(frozenset({owner}))
        async with dbutil.actor(owner) as session:
            chunks = (
                await session.exec(
                    select(func.count()).select_from(Chunk).where(Chunk.text.ilike(f"%{marker}%"))
                )
            ).one()
            private_item = await session.get(SessionItem, private)
        async with dbutil.admin_engine().connect() as connection:
            blocked_at = await connection.scalar(
                select(SessionItem.promoted_at).where(SessionItem.id == blocked)
            )
        assert private_item is not None
        return (
            promoted,
            chunks or 0,
            private_item.promoted_at is not None,
            blocked_at is not None,
        )

    promoted, chunks, private_done, blocked_done = dbutil.run(body())
    assert promoted == 1  # only the writable private item is due
    assert chunks >= 1  # promotion reingested it into a graph chunk
    assert private_done is True  # and stamped it out of the working set
    assert blocked_done is False  # the read-only-scope item stays working


def test_promote_is_a_no_op_when_nothing_is_due(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_tier_module, "enqueue_pending", noop_enqueue)

    async def body() -> int:
        owner = await seedgraph.fresh_owner()
        return await promote_sessions(frozenset({owner}))

    assert dbutil.run(body()) == 0


def test_promote_defaults_to_the_system_user_on_an_empty_working_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_tier_module, "enqueue_pending", noop_enqueue)

    async def body() -> int:
        await dbutil.reset_db()
        return await promote_sessions()

    assert dbutil.run(body()) == 0
