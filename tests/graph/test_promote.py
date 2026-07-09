import uuid

import dbutil
import pytest
from sqlalchemy import text

from aizk.exceptions import NotVisibleError
from aizk.graph.promote import promote
from aizk.store import (
    Chunk,
    Document,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    acting_as,
)
from aizk.store.engine import caller_standing
from aizk.store.identity import org_uuid

pytestmark = pytest.mark.usefixtures("migrated_db")

UNIT_VECTOR = [1.0] + [0.0] * 1023


async def seed_source(promoter: uuid.UUID) -> uuid.UUID:
    """Plant a private document with one chunk, one entity, and one fact owned by the promoter."""
    document, chunk, entity = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with acting_as(promoter) as session:
        session.add(
            Document(id=document, content_hash="promote", owner_id=promoter, title="source")
        )
        session.add(Chunk(id=chunk, document_id=document, ord=0, text="span", owner_id=promoter))
        session.add(EntityContent(id=entity, name="Leech", type="concept", embedding=UNIT_VECTOR))
        await session.flush()
        session.add(EntityClaim(content_id=entity, owner_id=promoter))
        content = FactContent(
            subject_id=entity,
            predicate="related_to",
            statement="the source fact",
            embedding=UNIT_VECTOR,
        )
        session.add(content)
        await session.flush()
        session.add(FactClaim(content_id=content.id, owner_id=promoter, source_chunk_id=chunk))
    return document


async def visible_copy(
    reader: uuid.UUID, source: uuid.UUID, orgs: tuple[uuid.UUID, ...] = ()
) -> uuid.UUID | None:
    """The id of the promoted copy a reader sees under its org standing, null when blind to it."""
    with caller_standing(orgs, ()):
        async with acting_as(reader) as session:
            return await session.scalar(
                text("SELECT id FROM document WHERE promoted_from = :src"), {"src": source}
            )


def test_promote_copies_into_scope_and_an_outsider_stays_blind() -> None:
    """A copy lands in the target org a member reads, the outsider stays blind, the source private.

    The count sums the document, its one chunk, and its one fact, and the copy points back at the
    source through promoted_from. The owner and a member standing in the target org read the same
    copy, an outsider with no standing reads nothing, and the source keeps its private empty scope
    set, never widened by the promotion.
    """

    async def probe() -> tuple[int, uuid.UUID | None, uuid.UUID | None, uuid.UUID | None, list]:
        await dbutil.reset_db()
        promoter, member, outsider = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        team_org = f"team-{uuid.uuid4()}"
        team_scope = org_uuid(team_org)
        source = await seed_source(promoter)
        count = await promote(source, team_org, user_id=promoter)
        async with acting_as(promoter) as session:
            source_scopes = await session.scalar(
                text("SELECT scopes FROM document WHERE id = :id"), {"id": source}
            )
        return (
            count,
            await visible_copy(promoter, source),
            await visible_copy(member, source, (team_scope,)),
            await visible_copy(outsider, source),
            list(source_scopes),
        )

    count, promoter_sees, member_sees, outsider_sees, source_scopes = dbutil.run(probe())
    assert count == 3  # the document, its one chunk, and its one fact
    assert promoter_sees is not None  # the owner reads its own copy regardless of standing
    assert member_sees == promoter_sees  # a member standing in the target org reads the same copy
    assert outsider_sees is None  # no standing in the target org, no copy
    assert source_scopes == []  # the source stays private, never widened by the promotion


def test_promote_of_an_invisible_document_raises() -> None:
    """A promoter promoting a document they cannot see is refused before any copy is written."""

    async def probe() -> None:
        await dbutil.reset_db()
        promoter = uuid.uuid4()
        team_org = f"team-{uuid.uuid4()}"
        with pytest.raises(NotVisibleError, match="no visible document"):
            await promote(uuid.uuid4(), team_org, user_id=promoter)

    dbutil.run(probe())
