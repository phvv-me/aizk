import dbutil
import pytest
import seedgraph
from id_factory import uuid5
from pydantic import UUID5, UUID7
from sqlmodel import select

import aizk.graph.session_tier as session_tier_module
from aizk.config import settings
from aizk.graph.session_tier import promote_sessions
from aizk.store import Chunk, SessionItem
from aizk.types import Scopes

pytestmark = pytest.mark.usefixtures("migrated_db", "fake_embedder")


async def noop_enqueue(
    limit: int | None = None,
    scopes: Scopes | None = None,
    source: str | None = None,
) -> int:
    del limit, scopes, source
    return 0


async def seed_item(
    owner: UUID5 | UUID7, text: str, scopes: tuple[UUID5 | UUID7, ...] = ()
) -> UUID5 | UUID7:
    item_id = uuid5()
    scopes = scopes or (owner,)
    await dbutil.admin_exec(
        "INSERT INTO session_item (id, created_by, scopes, kind, text) "
        "VALUES (:id, :owner, CAST(:scopes AS uuid[]), 'note', :text)",
        {"id": item_id, "owner": owner, "scopes": [str(s) for s in scopes], "text": text},
    )
    return item_id


@pytest.mark.parametrize("scenario", ["due", "explicit-empty", "default-empty"])
def test_promote_sessions_handles_due_writable_items_and_empty_working_sets(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    monkeypatch.setattr(session_tier_module, "enqueue_pending", noop_enqueue)
    if scenario == "due":
        monkeypatch.setattr(settings, "session_promote_age_minutes", 0.0)
        marker = uuid5().hex

        async def promote_due() -> tuple[int, int, bool, bool]:
            owner = await seedgraph.fresh_owner()
            readonly = uuid5()
            private = await seed_item(owner, f"a decision about {marker} worth keeping")
            blocked = await seed_item(owner, f"team note {marker}", scopes=(readonly,))
            promoted = await promote_sessions(frozenset({owner}))
            async with dbutil.actor(owner) as session:
                chunks = (
                    await session.exec(
                        select(Chunk.id.count()).where(Chunk.text.ilike(f"%{marker}%"))
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

        promoted, chunks, private_done, blocked_done = dbutil.run(promote_due())
        assert (promoted, private_done, blocked_done) == (1, True, False)
        assert chunks >= 1
        return

    async def promote_empty() -> int:
        if scenario == "default-empty":
            await dbutil.reset_db()
            return await promote_sessions()
        owner = await seedgraph.fresh_owner()
        return await promote_sessions(frozenset({owner}))

    assert dbutil.run(promote_empty()) == 0
