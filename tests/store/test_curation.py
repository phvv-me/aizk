import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import NamedTuple

import pytest
from graphdb import (
    add_member,
    approve_facts,
    create_group,
    curate_group,
    delete_group,
    drop_principals,
    pending_facts,
    purge_owner,
    reject_facts,
    require_group_admin,
    review_stamp,
)
from sqlalchemy import select, text

from aizk.cli import migrate
from aizk.config import settings
from aizk.exceptions import NotGroupAdminError
from aizk.extract.models import TimedFact
from aizk.graph import promote
from aizk.graph.build import GraphWriter
from aizk.store import (
    Chunk,
    Document,
    EntityClaim,
    EntityContent,
    FactClaim,
    FactContent,
    Group,
    acting_as,
    async_session,
)


class CuratedBrain(NamedTuple):
    """A curated group with two writers, an admin, a reader, an outsider, and a server admin.

    writer: writer-role member, may write but never review its own pending facts into canon.
    writer2: a second writer-role member, the "other writer" a pending fact stays hidden from.
    admin: admin-role member, may review the group's pending queue.
    reader: reader-role member, visibility without write access.
    outsider: principal with no membership at all.
    server_admin: principal with the server-wide `is_admin` flag and no membership in the group.
    group: the curated scope bridging every member above.
    """

    writer: uuid.UUID
    writer2: uuid.UUID
    admin: uuid.UUID
    reader: uuid.UUID
    outsider: uuid.UUID
    server_admin: uuid.UUID
    group: uuid.UUID


@asynccontextmanager
async def curated_brain() -> AsyncIterator[CuratedBrain]:
    """Yield a seeded curated group with every standing curation cares about, torn down on exit."""
    migrate()
    writer, writer2, admin, reader, outsider, server_admin = (uuid.uuid4() for _ in range(6))
    async with async_session()() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO principal (id, is_admin) VALUES "
                "(:w, false), (:w2, false), (:a, false), "
                "(:r, false), (:o, false), (:s, true)"
            ),
            {
                "w": writer,
                "w2": writer2,
                "a": admin,
                "r": reader,
                "o": outsider,
                "s": server_admin,
            },
        )
    group = await create_group(f"curated-{uuid.uuid4().hex[:8]}", curated=True)
    await add_member(writer, group, role="writer")
    await add_member(writer2, group, role="writer")
    await add_member(admin, group, role="admin")
    await add_member(reader, group, role="reader")
    try:
        yield CuratedBrain(writer, writer2, admin, reader, outsider, server_admin, group)
    finally:
        await delete_group(group)
        for principal in (writer, writer2, admin, reader, outsider, server_admin):
            await purge_owner(principal)
        await drop_principals(writer, writer2, admin, reader, outsider, server_admin)


async def plant_fact(
    owner: uuid.UUID, scope: uuid.UUID | None, statement: str, reviewed_at: datetime | None
) -> uuid.UUID:
    """Insert one entity and a fact naming it, stamped with a given reviewed_at, its claim id back.

    Bypasses `GraphWriter` so a visibility test exercises exactly the read gate, independent of
    the write-path stamping `GraphWriter.reviewed_at` covers separately. Plants a content row and
    this owner's claim on it for both the entity and the fact, mirroring the two-insert shape
    production writes use.

    owner: principal that owns both claims.
    scope: group the claims are shared with, private when null.
    statement: the fact's natural-language statement, unique per call so ids never collide.
    reviewed_at: the review stamp to test the read gate against.
    """
    entity_content, entity_claim = uuid.uuid4(), uuid.uuid4()
    fact_content, fact_claim = uuid.uuid4(), uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(EntityContent(id=entity_content, name=statement, type="Concept"))
        # a bare FK column, with no relationship() between EntityContent and EntityClaim, gives
        # the unit of work no dependency to sort on, so the explicit flush is what guarantees the
        # content row exists before the claim's FK insert runs, same for fact content below.
        await session.flush()
        session.add(
            EntityClaim(id=entity_claim, content_id=entity_content, owner_id=owner, scope=scope)
        )
        session.add(
            FactContent(
                id=fact_content,
                subject_id=entity_content,
                predicate="related_to",
                statement=statement,
            )
        )
        await session.flush()
        session.add(
            FactClaim(
                id=fact_claim,
                content_id=fact_content,
                owner_id=owner,
                scope=scope,
                reviewed_at=reviewed_at,
            )
        )
    return fact_claim


async def fact_visible(reader: uuid.UUID, fact_id: uuid.UUID) -> bool:
    """Whether a reader's ordinary, gated session can see a fact claim by id.

    reader: identity the read acts as.
    fact_id: fact claim being probed for visibility.
    """
    async with acting_as(reader) as session:
        return (
            await session.scalar(select(FactClaim.id).where(FactClaim.id == fact_id)) is not None
        )


async def seed_chunk(owner: uuid.UUID, scope: uuid.UUID | None) -> uuid.UUID:
    """Plant a document and one chunk `GraphWriter.consolidate` can stamp as a fact's source.

    owner: principal that owns the document and chunk.
    scope: group the rows are shared with, private when null.
    """
    document, chunk = uuid.uuid4(), uuid.uuid4()
    async with acting_as(owner) as session:
        session.add(
            Document(id=document, content_hash=uuid.uuid4().hex, owner_id=owner, scope=scope)
        )
        session.add(
            Chunk(id=chunk, document_id=document, ord=0, text="span", owner_id=owner, scope=scope)
        )
    return chunk


async def seed_source(owner: uuid.UUID) -> uuid.UUID:
    """Plant a private document with one chunk, entity, and live fact owned by owner, its id back.

    owner: identity that owns every row, the promoter in the promote-into-curated-group tests.
    """
    document, chunk, entity_content, entity_claim, fact_content = (uuid.uuid4() for _ in range(5))
    async with acting_as(owner) as session:
        session.add(Document(id=document, content_hash=uuid.uuid4().hex, owner_id=owner))
        session.add(Chunk(id=chunk, document_id=document, ord=0, text="span", owner_id=owner))
        session.add(EntityContent(id=entity_content, name="Promotable", type="Concept"))
        await session.flush()
        session.add(EntityClaim(id=entity_claim, content_id=entity_content, owner_id=owner))
        session.add(
            FactContent(
                id=fact_content,
                subject_id=entity_content,
                predicate="related_to",
                statement="a promotable fact",
            )
        )
        await session.flush()
        session.add(
            FactClaim(
                id=uuid.uuid4(),
                content_id=fact_content,
                owner_id=owner,
                source_chunk_id=chunk,
                reviewed_at=None,
            )
        )
    return document


async def group_name_of(group_id: uuid.UUID) -> str:
    """Read a group's name back from its id, the promote tool's own name-first interface.

    group_id: group whose name is resolved.
    """
    async with async_session()() as session, session.begin():
        return await session.scalar(select(Group.name).where(Group.id == group_id))


def test_writer_fact_in_a_curated_group_is_visible_only_to_its_author(requires_db: None) -> None:
    """A pending fact is invisible to a fellow writer and a reader, visible only to its author."""

    async def probe() -> tuple[bool, bool, bool]:
        async with curated_brain() as brain:
            fact_id = await plant_fact(brain.writer, brain.group, "pending claim", None)
            author_sees = await fact_visible(brain.writer, fact_id)
            peer_sees = await fact_visible(brain.writer2, fact_id)
            reader_sees = await fact_visible(brain.reader, fact_id)
            return author_sees, peer_sees, reader_sees

    author_sees, peer_sees, reader_sees = asyncio.run(probe())
    assert author_sees is True
    assert peer_sees is False
    assert reader_sees is False


@pytest.mark.parametrize("approve_all", [False, True], ids=["by-id", "all-pending"])
def test_admin_reviews_pending_via_pending_and_approve_reveals_it_to_everyone(
    requires_db: None, approve_all: bool
) -> None:
    """`pending` lists an unreviewed fact, and `approve` opens it to every member at once.

    Approving a named id and approving every still-pending fact (the `None` selector the `approve
    all` tool sends) both clear the one pending fact planted into this fresh group, since the group
    is minted per run so its only pending fact is this one.
    """

    async def probe() -> tuple[set[uuid.UUID], int, bool, bool]:
        async with curated_brain() as brain:
            fact_id = await plant_fact(brain.writer, brain.group, "reviewable claim", None)
            queue = {fact.id for fact in await pending_facts(brain.group)}
            approved = await approve_facts(brain.group, None if approve_all else [fact_id])
            peer_sees = await fact_visible(brain.writer2, fact_id)
            reader_sees = await fact_visible(brain.reader, fact_id)
            return queue, approved, peer_sees, reader_sees

    queue, approved, peer_sees, reader_sees = asyncio.run(probe())
    assert queue  # the pending fact planted this run is somewhere in the queue
    assert approved == 1
    assert peer_sees is True
    assert reader_sees is True


def test_reject_deletes_the_pending_fact_before_it_ever_becomes_canon(requires_db: None) -> None:
    """A rejected fact is removed outright, never merely hidden."""

    async def probe() -> tuple[int, bool]:
        async with curated_brain() as brain:
            fact_id = await plant_fact(brain.writer, brain.group, "rejected claim", None)
            rejected = await reject_facts(brain.group, [fact_id])
            async with acting_as(brain.writer) as session:
                still_there = (
                    await session.get(
                        FactClaim, fact_id, execution_options={settings.skip_live_gate: True}
                    )
                    is not None
                )
            return rejected, still_there

    rejected, still_there = asyncio.run(probe())
    assert rejected == 1
    assert still_there is False


def test_uncurated_group_and_private_scope_stamp_immediately(requires_db: None) -> None:
    """review_stamp resolves to now for a private write and for a write into an uncurated group."""

    async def probe() -> tuple[datetime | None, datetime | None, datetime | None, datetime | None]:
        async with curated_brain() as brain:
            uncurated = await create_group(f"open-{uuid.uuid4().hex[:8]}")
            await add_member(brain.writer, uncurated, role="writer")
            try:
                async with acting_as(brain.writer) as session:
                    private_stamp = await review_stamp(session, None, brain.writer)
                    open_stamp = await review_stamp(session, uncurated, brain.writer)
                    curated_writer_stamp = await review_stamp(session, brain.group, brain.writer)
                    curated_admin_stamp = await review_stamp(session, brain.group, brain.admin)
                return private_stamp, open_stamp, curated_writer_stamp, curated_admin_stamp
            finally:
                await delete_group(uncurated)

    private_stamp, open_stamp, curated_writer_stamp, curated_admin_stamp = asyncio.run(probe())
    assert private_stamp is not None
    assert open_stamp is not None
    assert curated_writer_stamp is None
    assert curated_admin_stamp is not None


@pytest.mark.usefixtures("fake_embedder")
def test_graph_writer_stamps_pending_for_a_writer_and_live_for_a_group_admin(
    requires_db: None,
) -> None:
    """The real write path, `GraphWriter.consolidate`, lands a writer's fact pending, admin's
    live.
    """

    async def probe() -> tuple[datetime | None, datetime | None]:
        async with curated_brain() as brain:
            writer_chunk = await seed_chunk(brain.writer, brain.group)
            async with acting_as(brain.writer) as session:
                writer_gw = GraphWriter(session, brain.writer, brain.group)
                await writer_gw.resolve("Pending Thing", "Concept")
                await writer_gw.consolidate(
                    TimedFact(
                        subject="Pending Thing", predicate="related_to", statement="pending stmt"
                    ),
                    writer_chunk,
                )
            admin_chunk = await seed_chunk(brain.admin, brain.group)
            async with acting_as(brain.admin) as session:
                admin_gw = GraphWriter(session, brain.admin, brain.group)
                await admin_gw.resolve("Reviewed Thing", "Concept")
                await admin_gw.consolidate(
                    TimedFact(
                        subject="Reviewed Thing", predicate="related_to", statement="reviewed stmt"
                    ),
                    admin_chunk,
                )
            async with acting_as(brain.admin) as session:
                pending_stamp = await session.scalar(
                    select(FactClaim.reviewed_at)
                    .join(FactContent, FactClaim.content_id == FactContent.id)
                    .where(FactContent.statement == "pending stmt")
                    .execution_options(**{settings.skip_live_gate: True})
                )
                reviewed_stamp = await session.scalar(
                    select(FactClaim.reviewed_at)
                    .join(FactContent, FactClaim.content_id == FactContent.id)
                    .where(FactContent.statement == "reviewed stmt")
                )
            return pending_stamp, reviewed_stamp

    pending_stamp, reviewed_stamp = asyncio.run(probe())
    assert pending_stamp is None
    assert reviewed_stamp is not None


def test_promote_into_a_curated_group_lands_pending_for_a_non_admin_and_live_for_an_admin(
    requires_db: None,
) -> None:
    """A non-admin promoter's copy lands pending, an admin promoter's copy is live at once."""

    async def probe() -> tuple[datetime | None, datetime | None]:
        async with curated_brain() as brain:
            group_name = await group_name_of(brain.group)
            writer_source = await seed_source(brain.writer)
            await promote(writer_source, group_name, principal_id=brain.writer)
            admin_source = await seed_source(brain.admin)
            await promote(admin_source, group_name, principal_id=brain.admin)
            async with acting_as(brain.writer) as session:
                writer_copy_stamp = await session.scalar(
                    select(FactClaim.reviewed_at)
                    .where(FactClaim.owner_id == brain.writer, FactClaim.scope == brain.group)
                    .execution_options(**{settings.skip_live_gate: True})
                )
                admin_copy_stamp = await session.scalar(
                    select(FactClaim.reviewed_at)
                    .where(FactClaim.owner_id == brain.admin, FactClaim.scope == brain.group)
                    .execution_options(**{settings.skip_live_gate: True})
                )
            return writer_copy_stamp, admin_copy_stamp

    writer_copy_stamp, admin_copy_stamp = asyncio.run(probe())
    assert writer_copy_stamp is None
    assert admin_copy_stamp is not None


def test_skip_live_gate_still_dumps_a_pending_row_regardless_of_reader(requires_db: None) -> None:
    """The export and history lane's opt-out sees a pending fact even when the reader is not its
    author.
    """

    async def probe() -> tuple[bool, bool]:
        async with curated_brain() as brain:
            fact_id = await plant_fact(brain.writer, brain.group, "gate probe claim", None)
            async with acting_as(brain.reader) as session:
                hidden = await session.scalar(select(FactClaim.id).where(FactClaim.id == fact_id))
                shown = await session.scalar(
                    select(FactClaim.id)
                    .where(FactClaim.id == fact_id)
                    .execution_options(**{settings.skip_live_gate: True})
                )
            return hidden is None, shown == fact_id

    hidden, shown = asyncio.run(probe())
    assert hidden is True
    assert shown is True


def test_require_group_admin_passes_a_group_admin_and_a_server_admin_refuses_others(
    requires_db: None,
) -> None:
    """The group's own admin and any server admin pass, an ordinary outsider is refused."""

    async def probe() -> None:
        async with curated_brain() as brain:
            await require_group_admin(brain.admin, brain.group)
            await require_group_admin(brain.server_admin, brain.group)
            with pytest.raises(NotGroupAdminError):
                await require_group_admin(brain.outsider, brain.group)
            with pytest.raises(NotGroupAdminError):
                await require_group_admin(brain.writer, brain.group)

    asyncio.run(probe())


def test_server_admin_can_always_approve_even_without_group_membership(requires_db: None) -> None:
    """A server admin's own membership is irrelevant, `approve_facts` still reaches the group."""

    async def probe() -> int:
        async with curated_brain() as brain:
            fact_id = await plant_fact(brain.writer, brain.group, "server admin claim", None)
            await require_group_admin(brain.server_admin, brain.group)
            return await approve_facts(brain.group, [fact_id])

    assert asyncio.run(probe()) == 1


def test_curate_group_flips_the_flag(requires_db: None) -> None:
    """curate_group toggles a group between review-gated and ordinary immediate writes."""

    async def probe() -> tuple[datetime | None, datetime | None]:
        async with curated_brain() as brain:
            async with acting_as(brain.writer) as session:
                before = await review_stamp(session, brain.group, brain.writer)
            await curate_group(brain.group, curated=False)
            async with acting_as(brain.writer) as session:
                after = await review_stamp(session, brain.group, brain.writer)
            return before, after

    before, after = asyncio.run(probe())
    assert before is None
    assert after is not None
