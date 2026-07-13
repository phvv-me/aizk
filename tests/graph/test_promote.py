import uuid

import dbutil
import pytest
from sqlalchemy import text

from aizk.config import settings
from aizk.exceptions import NotVisibleError
from aizk.graph.promote import promote
from aizk.store import (
    Chunk,
    Document,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
)
from aizk.store.identity import User

pytestmark = pytest.mark.usefixtures("migrated_db")

UNIT_VECTOR = [1.0] + [0.0] * 1023


async def seed_source(promoter: uuid.UUID) -> uuid.UUID:
    document, chunk, entity = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with dbutil.actor(promoter) as session:
        session.add(
            Document(
                id=document,
                content_hash="promote",
                created_by=promoter,
                scopes=[promoter],
                title="source",
            )
        )
        session.add(
            Chunk(
                id=chunk,
                document_id=document,
                ord=0,
                text="span",
                created_by=promoter,
                scopes=[promoter],
            )
        )
        session.add(EntityContent(id=entity, name="Leech", type="concept", embedding=UNIT_VECTOR))
        await session.flush()
        session.add(EntityClaim(content_id=entity, created_by=promoter, scopes=[promoter]))
        content = FactContent(
            subject_id=entity,
            predicate="related_to",
            statement="the source fact",
            embedding=UNIT_VECTOR,
        )
        session.add(content)
        await session.flush()
        session.add(
            FactClaim(
                content_id=content.id,
                created_by=promoter,
                scopes=[promoter],
                source_chunk_id=chunk,
            )
        )
    return document


async def visible_copy(
    reader: uuid.UUID, source: uuid.UUID, orgs: tuple[uuid.UUID, ...] = ()
) -> uuid.UUID | None:
    user = User.authorized(reader, read=(reader, *orgs))
    async with user as session:
        return (
            await session.exec(
                text("SELECT id FROM document WHERE promoted_from = :src"),
                params={"src": source},
            )
        ).scalar_one_or_none()


def test_promote_copies_once_into_scope_and_an_outsider_stays_blind() -> None:
    async def probe() -> tuple[
        int, int, uuid.UUID | None, uuid.UUID | None, uuid.UUID | None, list
    ]:
        await dbutil.reset_db()
        promoter, member, outsider = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        team_org = f"team-{uuid.uuid4()}"
        team_scope = settings.scope_id(team_org)
        source = await seed_source(promoter)
        user = User.authorized(
            promoter,
            read=(promoter, team_scope),
            write=(promoter, team_scope),
        )
        count = await promote([source], frozenset({team_scope}), user)
        repeated = await promote([source], frozenset({team_scope}), user)
        async with dbutil.actor(promoter) as session:
            source_scopes = (
                await session.exec(
                    text("SELECT scopes FROM document WHERE id = :id"), params={"id": source}
                )
            ).scalar_one()
        return (
            count,
            repeated,
            await visible_copy(promoter, source, (team_scope,)),
            await visible_copy(member, source, (team_scope,)),
            await visible_copy(outsider, source),
            list(source_scopes),
        )

    count, repeated, promoter_sees, member_sees, outsider_sees, source_scopes = dbutil.run(probe())
    assert count == 1 and repeated == 0
    assert promoter_sees is not None
    assert member_sees == promoter_sees  # a member standing in the target org reads the same copy
    assert outsider_sees is None  # no standing in the target org, no copy
    assert len(source_scopes) == 1


def test_promote_of_an_invisible_document_raises() -> None:
    async def probe() -> None:
        await dbutil.reset_db()
        promoter = uuid.uuid4()
        with pytest.raises(NotVisibleError, match="no visible document"):
            await promote([uuid.uuid4()], frozenset({promoter}), User.private(promoter))

    dbutil.run(probe())
