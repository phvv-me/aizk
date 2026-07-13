import uuid

import dbutil
from sqlmodel import func, select

from aizk.eval.cleanup import purge_scope
from aizk.extract import ontology
from aizk.store import EntityClaim, EntityContent, FactClaim, FactContent
from aizk.store.engine import bypass_rls


def test_purge_scope_preserves_shared_content_until_its_last_claim_is_removed(
    migrated_db: None,
) -> None:
    async def body() -> None:
        await dbutil.reset_db()
        first, second = uuid.uuid7(), uuid.uuid7()
        entity = EntityContent(name="shared entity", type=ontology.CONCEPT)
        fact = FactContent(
            subject_id=entity.id,
            predicate=ontology.RELATED_TO,
            statement="The shared entity relates to memory.",
        )
        async with bypass_rls() as opened:
            opened.add(entity)
            await opened.flush()
            opened.add(fact)
            await opened.flush()
            opened.add_all(
                [
                    EntityClaim(content_id=entity.id, created_by=owner, scopes=[owner])
                    for owner in (first, second)
                ]
                + [
                    FactClaim(content_id=fact.id, created_by=owner, scopes=[owner])
                    for owner in (first, second)
                ]
            )
            await opened.commit()

        await purge_scope(frozenset({first}))
        async with bypass_rls() as opened:
            assert await opened.scalar(select(func.count()).select_from(EntityContent)) == 1
            assert await opened.scalar(select(func.count()).select_from(FactContent)) == 1
            assert await opened.scalar(select(func.count()).select_from(EntityClaim)) == 1
            assert await opened.scalar(select(func.count()).select_from(FactClaim)) == 1

        await purge_scope(frozenset({second}))
        async with bypass_rls() as opened:
            assert await opened.scalar(select(func.count()).select_from(EntityContent)) == 0
            assert await opened.scalar(select(func.count()).select_from(FactContent)) == 0

    dbutil.run(body())
