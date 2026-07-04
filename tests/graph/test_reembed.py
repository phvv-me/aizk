import uuid

import dbutil
import pytest
import seedgraph

from aizk.graph.reembed import reembed
from aizk.store import EntityContent, FactContent, acting_as

pytestmark = pytest.mark.usefixtures("migrated_db", "fake_embedder")


async def seed_one_of_each(owner: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Plant one unembedded row in every embedded table, return the entity and fact content ids.

    owner: principal that owns every seeded row.
    """
    await seedgraph.seed_scoped_row(owner, "chunk")
    await seedgraph.seed_scoped_row(owner, "community")
    await seedgraph.seed_scoped_row(owner, "profile")
    async with acting_as(owner) as session:
        entity = await seedgraph.add_entity(session, owner, "Ada", type="Author")
        fact, _ = await seedgraph.add_fact(session, owner, entity, statement="Ada relates memory")
    return entity, fact


def test_reembed_rewrites_every_embedded_table() -> None:
    """The migration walks the three per-tenant tables and both content tables, filling vectors.

    Chunk, Community, and Profile re-embed under the owner's own row level security and the two
    content tables re-embed system-wide through the admin connection, so on a freshly reset schema
    the count spans the three per-tenant rows, the two entity content rows (the fact's subject and
    the profile's subject), and the one fact content row, and both content rows carry a vector.
    """

    async def body() -> tuple[int, bool, bool]:
        owner = await seedgraph.fresh_owner()
        entity_id, fact_id = await seed_one_of_each(owner)
        total = await reembed(owner)
        async with acting_as(owner) as session:
            entity_row = await session.get(EntityContent, entity_id)
            fact_row = await session.get(FactContent, fact_id)
            assert entity_row is not None and fact_row is not None
            return total, entity_row.embedding is not None, fact_row.embedding is not None

    total, entity_embedded, fact_embedded = dbutil.run(body())
    assert total == 6  # chunk, community, profile, two entity content, one fact content
    assert entity_embedded is True
    assert fact_embedded is True
