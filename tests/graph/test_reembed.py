import asyncio
import uuid

import pytest
from graphdb import owned_principal

from aizk.graph.reembed import reembed
from aizk.store import (
    Chunk,
    Community,
    Document,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    Profile,
    acting_as,
)


async def seed_one_of_each(owner: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Plant one unembedded row in each embedded table, return the entity's and fact's content ids.

    owner: principal that owns every seeded row.
    """
    document, chunk, entity = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(Document(id=document, content_hash="reembed", owner_id=owner, title="doc"))
        session.add(Chunk(id=chunk, document_id=document, ord=0, text="a span", owner_id=owner))
        session.add(EntityContent(id=entity, name="Ada", type="Author", embedding=None))
        await session.flush()
        session.add(EntityClaim(content_id=entity, owner_id=owner))
        fact_content = FactContent(
            subject_id=entity,
            predicate="related_to",
            statement="Ada relates to memory",
            embedding=None,
        )
        session.add(fact_content)
        await session.flush()
        session.add(FactClaim(content_id=fact_content.id, owner_id=owner))
        session.add(
            Community(
                id=uuid.uuid4(), owner_id=owner, label="theme", summary="a summary", embedding=None
            )
        )
        session.add(
            Profile(id=uuid.uuid4(), owner_id=owner, subject_id=entity, summary="a portrait")
        )
    return entity, fact_content.id


@pytest.mark.usefixtures("fake_embedder", "fake_settings")
def test_reembed_rewrites_every_embedded_table() -> None:
    """The migration walks the per-tenant tables plus both content tables, rewriting each
    unembedded row's vector in place.

    Chunk, Community, and Profile re-embed under the owner's own row level security, so their
    combined count is exactly the three seeded rows; EntityContent and FactContent re-embed
    system-wide through the admin connection, so their share of the count is not pinned since
    other principals' content lives in the same two tables. The seeded entity's and fact's own
    content rows are read back directly instead, each now carrying a vector where it held none.
    """

    async def probe() -> tuple[int, bool, bool]:
        async with owned_principal() as owner:
            entity_id, fact_content_id = await seed_one_of_each(owner)
            total = await reembed(owner)
            async with acting_as(owner) as session:
                entity_row = await session.get(EntityContent, entity_id)
                fact_row = await session.get(FactContent, fact_content_id)
                assert entity_row is not None
                assert fact_row is not None
                return total, entity_row.embedding is not None, fact_row.embedding is not None

    total, entity_embedded, fact_embedded = asyncio.run(probe())
    assert total >= 5  # the three per-tenant rows plus at least this test's two content rows
    assert entity_embedded is True
    assert fact_embedded is True
