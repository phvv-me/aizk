import uuid
from collections.abc import Iterator
from typing import NamedTuple

import dbutil
import pytest
from sqlalchemy import text

from aizk.exceptions import NotVisibleError, ScopeNotFoundError
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

UNIT_VECTOR = [1.0] + [0.0] * 1023


class Grid(NamedTuple):
    """The principals and the bridging team one promotion is probed against."""

    promoter: uuid.UUID
    member: uuid.UUID
    outsider: uuid.UUID
    team: uuid.UUID
    team_name: str


@pytest.fixture
def grid(migrated_db: None) -> Iterator[Grid]:
    """A reset schema seeding three principals and one team, memberships added per test."""
    lattice = Grid(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), f"team-{uuid.uuid4()}")

    async def setup() -> None:
        await dbutil.reset_db()
        for principal in (lattice.promoter, lattice.member, lattice.outsider):
            await dbutil.seed_user(principal)
        await dbutil.seed_group(lattice.team, name=lattice.team_name)

    dbutil.run(setup())
    yield lattice


async def seed_source(promoter: uuid.UUID) -> uuid.UUID:
    """Plant a private document with one chunk, one entity, and one fact owned by the promoter."""
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
    """The id of the promoted copy a reader can see, null when blind to it."""
    async with acting_as(reader) as session:
        return await session.scalar(
            text("SELECT id FROM document WHERE promoted_from = :src"), {"src": source}
        )


def test_promote_copies_into_scope_and_an_outsider_stays_blind(grid: Grid) -> None:
    """A copy lands in team scope a member reads, the outsider stays blind, the source untouched.

    The count sums the document, its chunks, and its facts, the copy points back at the source
    through promoted_from, a team member reads the same copy, an outsider in no shared group reads
    nothing, and the source keeps its private empty scope set, never widened by the promotion.
    """

    async def probe() -> tuple[int, uuid.UUID | None, uuid.UUID | None, uuid.UUID | None, list]:
        await dbutil.seed_membership(grid.promoter, grid.team, "writer")
        await dbutil.seed_membership(grid.member, grid.team, "reader")
        source = await seed_source(grid.promoter)
        count = await promote(source, grid.team_name, principal_id=grid.promoter)
        async with acting_as(grid.promoter) as session:
            source_scopes = await session.scalar(
                text("SELECT scopes FROM document WHERE id = :id"), {"id": source}
            )
        return (
            count,
            await visible_copy(grid.promoter, source),
            await visible_copy(grid.member, source),
            await visible_copy(grid.outsider, source),
            list(source_scopes),
        )

    count, promoter_sees, member_sees, outsider_sees, source_scopes = dbutil.run(probe())
    assert count == 3  # the document, its one chunk, and its one fact
    assert promoter_sees is not None
    assert member_sees == promoter_sees
    assert outsider_sees is None
    assert source_scopes == []


def test_promote_into_an_unknown_scope_raises(grid: Grid) -> None:
    """Naming a group that does not exist fails before any copy is written."""

    async def probe() -> None:
        await dbutil.seed_membership(grid.promoter, grid.team, "writer")
        with pytest.raises(ScopeNotFoundError, match="no scope named"):
            await promote(uuid.uuid4(), "no such team", principal_id=grid.promoter)

    dbutil.run(probe())


def test_promote_by_a_non_member_raises(grid: Grid) -> None:
    """Membership, not the scope predicate alone, is the publish right, so a non-member fails."""

    async def probe() -> None:
        with pytest.raises(ValueError, match="may not publish"):
            await promote(uuid.uuid4(), grid.team_name, principal_id=grid.promoter)

    dbutil.run(probe())


def test_promote_of_an_invisible_document_raises(grid: Grid) -> None:
    """A member promoting a document they cannot see is refused before any copy is written."""

    async def probe() -> None:
        await dbutil.seed_membership(grid.promoter, grid.team, "writer")
        with pytest.raises(NotVisibleError, match="no visible document"):
            await promote(uuid.uuid4(), grid.team_name, principal_id=grid.promoter)

    dbutil.run(probe())
