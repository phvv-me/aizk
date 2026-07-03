import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NamedTuple

import pytest
from graphdb import add_principals, drop_principals, purge_owner
from sqlalchemy import text

from aizk.graph.promote import promote
from aizk.store import (
    Chunk,
    Document,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    acting_as,
    async_session,
)

UNIT_VECTOR = [1.0] + [0.0] * 1023


class Lattice(NamedTuple):
    """The principals and the bridging group one promotion is probed against.

    promoter: owner of the private source and a member of the target team.
    member: a second principal in the target team who owns nothing.
    outsider: a principal in no shared group, who must stay blind to the copy.
    team: the target group's id.
    team_name: the group's name, the surface form promote resolves the scope from.
    """

    promoter: uuid.UUID
    member: uuid.UUID
    outsider: uuid.UUID
    team: uuid.UUID
    team_name: str


@asynccontextmanager
async def lattice(*, joined: bool = True) -> AsyncIterator[Lattice]:
    """Seed three principals and a team the promoter and member share, tearing it all down on exit.

    joined: whether the promoter is enrolled in the team, false to probe the publish-right guard.
    """
    grid = Lattice(
        promoter=uuid.uuid4(),
        member=uuid.uuid4(),
        outsider=uuid.uuid4(),
        team=uuid.uuid4(),
        team_name=f"team {uuid.uuid4().hex}",
    )
    await add_principals(grid.promoter, grid.member, grid.outsider)
    async with async_session()() as session, session.begin():
        await session.execute(
            text("INSERT INTO group_ (id, name) VALUES (:t, :name)"),
            {"t": grid.team, "name": grid.team_name},
        )
        rows = [{"p": grid.member, "t": grid.team}]
        if joined:
            rows.append({"p": grid.promoter, "t": grid.team})
        await session.execute(
            text("INSERT INTO membership (principal_id, group_id) VALUES (:p, :t)"), rows
        )
    try:
        yield grid
    finally:
        for principal in (grid.promoter, grid.member, grid.outsider):
            await purge_owner(principal)
        async with async_session()() as session, session.begin():
            await session.execute(
                text("DELETE FROM membership WHERE group_id = :t"), {"t": grid.team}
            )
            await session.execute(text("DELETE FROM group_ WHERE id = :t"), {"t": grid.team})
        await drop_principals(grid.promoter, grid.member, grid.outsider)


async def seed_source(promoter: uuid.UUID) -> uuid.UUID:
    """Plant a private document with one chunk, one entity, and one fact owned by the promoter.

    promoter: owner of every private row, the only principal who may read it.
    """
    document, chunk, entity = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with acting_as(promoter) as session:
        session.add(
            Document(id=document, content_hash="promote", owner_id=promoter, title="source")
        )
        session.add(Chunk(id=chunk, document_id=document, ord=0, text="span", owner_id=promoter))
        session.add(EntityContent(id=entity, name="Leech", type="Concept", embedding=UNIT_VECTOR))
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


async def visible_copy(reader: uuid.UUID, source: uuid.UUID) -> uuid.UUID | None:
    """The id of the promoted copy a reader can see, null when blind to it.

    reader: principal whose visibility scopes the lookup.
    source: the original document the copy points back to.
    """
    async with acting_as(reader) as session:
        return await session.scalar(
            text("SELECT id FROM document WHERE promoted_from = :src"), {"src": source}
        )


@pytest.mark.usefixtures("migrated_db")
def test_promote_copies_into_scope_and_an_outsider_stays_blind() -> None:
    """A copy lands in team scope a member reads, the outsider stays blind, source untouched."""

    async def probe() -> tuple[
        int, uuid.UUID | None, uuid.UUID | None, uuid.UUID | None, uuid.UUID | None
    ]:
        async with lattice() as grid:
            source = await seed_source(grid.promoter)
            count = await promote(source, grid.team_name, principal_id=grid.promoter)
            async with acting_as(grid.promoter) as session:
                source_scope = await session.scalar(
                    text("SELECT scope FROM document WHERE id = :id"), {"id": source}
                )
            return (
                count,
                await visible_copy(grid.promoter, source),
                await visible_copy(grid.member, source),
                await visible_copy(grid.outsider, source),
                source_scope,
            )

    count, promoter_sees, member_sees, outsider_sees, source_scope = asyncio.run(probe())
    assert count >= 1
    assert promoter_sees is not None
    assert member_sees == promoter_sees
    assert outsider_sees is None
    assert source_scope is None


@pytest.mark.usefixtures("migrated_db")
def test_promote_into_an_unknown_scope_raises() -> None:
    """Naming a group that does not exist fails before any copy is written."""

    async def probe() -> None:
        async with lattice() as grid:
            with pytest.raises(ValueError, match="no scope named"):
                await promote(uuid.uuid4(), "no such team", principal_id=grid.promoter)

    asyncio.run(probe())


@pytest.mark.usefixtures("migrated_db")
def test_promote_by_a_non_member_raises() -> None:
    """Membership, not the scope predicate alone, is the publish right, so a non-member fails."""

    async def probe() -> None:
        async with lattice(joined=False) as grid:
            with pytest.raises(ValueError, match="may not publish"):
                await promote(uuid.uuid4(), grid.team_name, principal_id=grid.promoter)

    asyncio.run(probe())


@pytest.mark.usefixtures("migrated_db")
def test_promote_of_an_invisible_document_raises() -> None:
    """A member promoting a document they cannot see is refused before any copy is written."""

    async def probe() -> None:
        async with lattice() as grid:
            with pytest.raises(ValueError, match="no visible document"):
                await promote(uuid.uuid4(), grid.team_name, principal_id=grid.promoter)

    asyncio.run(probe())
