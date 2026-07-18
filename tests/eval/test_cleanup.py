import dbutil
from id_factory import uuid5
from sqlmodel import select

from aizk.ontology import System
from aizk.store import Entity, Fact
from aizk.store.identity import User
from eval.cleanup import purge_scope


def test_purge_scope_preserves_shared_content_until_its_last_claim_is_removed(
    migrated_db: None,
) -> None:
    async def body() -> None:
        await dbutil.reset_db()
        first, second = uuid5(), uuid5()
        entity = Entity.Content(id=uuid5(), name="shared entity", type=System.Entity.CONCEPT)
        fact = Fact.Content(
            id=uuid5(),
            subject_id=entity.id,
            predicate=System.Relation.RELATED_TO,
            statement="The shared entity relates to memory.",
        )
        async with User.system().owner as opened:
            opened.add(entity)
            await opened.flush()
            opened.add(fact)
            await opened.flush()
            opened.add_all(
                [
                    Entity.Claim(content_id=entity.id, created_by=owner, scopes=[owner])
                    for owner in (first, second)
                ]
                + [
                    Fact.Claim(content_id=fact.id, created_by=owner, scopes=[owner])
                    for owner in (first, second)
                ]
            )
            await opened.commit()

        await purge_scope(frozenset({first}))
        async with User.system().owner as opened:
            assert await opened.scalar(select(Entity.Content.id.count())) == 1
            assert await opened.scalar(select(Fact.Content.id.count())) == 1
            assert await opened.scalar(select(Entity.Claim.id.count())) == 1
            assert await opened.scalar(select(Fact.Claim.id.count())) == 1

        await purge_scope(frozenset({second}))
        async with User.system().owner as opened:
            assert await opened.scalar(select(Entity.Content.id.count())) == 0
            assert await opened.scalar(select(Fact.Content.id.count())) == 0

    dbutil.run(body())
