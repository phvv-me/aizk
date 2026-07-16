import dbutil
import pytest
import seedgraph
from pydantic import UUID5, UUID7

from aizk.graph.reembed import reembed
from aizk.store import Entity, Fact

pytestmark = pytest.mark.usefixtures("migrated_db", "fake_embedder")


async def seed_one_of_each(owner: UUID5 | UUID7) -> tuple[UUID5 | UUID7, UUID5 | UUID7]:
    await seedgraph.seed_scoped_row(owner, "chunk")
    await seedgraph.seed_scoped_row(owner, "community")
    await seedgraph.seed_scoped_row(owner, "profile")
    async with dbutil.actor(owner) as session:
        entity = await seedgraph.add_entity(session, owner, "Ada", type="author")
        fact, _ = await seedgraph.add_fact(session, owner, entity, statement="Ada relates memory")
    return entity, fact


def test_reembed_rewrites_every_embedded_table() -> None:
    async def body() -> tuple[int, bool, bool]:
        owner = await seedgraph.fresh_owner()
        entity_id, fact_id = await seed_one_of_each(owner)
        total = await reembed(frozenset({owner}))
        async with dbutil.actor(owner) as session:
            entity_row = await session.get(Entity.Content, entity_id)
            fact_row = await session.get(Fact.Content, fact_id)
            assert entity_row is not None and fact_row is not None
            return total, entity_row.embedding is not None, fact_row.embedding is not None

    total, entity_embedded, fact_embedded = dbutil.run(body())
    assert total == 6  # chunk, community, profile, two entity content, one fact content
    assert entity_embedded is True
    assert fact_embedded is True
