import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NamedTuple

import pytest
from graphdb import add_member, create_group, delete_group, publish_group, remove_member
from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError

from aizk.cli import migrate
from aizk.config import settings
from aizk.graph import promote
from aizk.store import Document, acting_as, async_session


class Brain(NamedTuple):
    """One shared-brain fixture: a group with a writer and a reader, plus an outsider.

    writer: member holding the writer role, allowed to write into the group scope.
    reader: member holding the reader role, visibility without write access.
    outsider: principal with no membership at all.
    group: the shared scope bridging writer and reader.
    """

    writer: uuid.UUID
    reader: uuid.UUID
    outsider: uuid.UUID
    group: uuid.UUID


@asynccontextmanager
async def shared_brain() -> AsyncIterator[Brain]:
    """Yield a seeded group with writer, reader, and outsider, removing everything on exit.

    Cleanup deletes the group first so its rows demote to private, then removes each principal's
    rows as that owner under the write policy, then the principals themselves.
    """
    migrate()
    writer, reader, outsider = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with async_session()() as session, session.begin():
        await session.execute(
            text("INSERT INTO principal (id) VALUES (:w), (:r), (:o)"),
            {"w": writer, "r": reader, "o": outsider},
        )
    group = await create_group(f"brain-{uuid.uuid4().hex[:8]}")
    await add_member(writer, group, role="writer")
    await add_member(reader, group, role="reader")
    try:
        yield Brain(writer=writer, reader=reader, outsider=outsider, group=group)
    finally:
        await delete_group(group)
        for principal in (writer, reader, outsider):
            async with acting_as(principal) as session:
                await session.execute(
                    text("DELETE FROM document WHERE owner_id = :p"), {"p": principal}
                )
        async with async_session()() as session, session.begin():
            await session.execute(
                text("DELETE FROM principal WHERE id IN (:w, :r, :o)"),
                {"w": writer, "r": reader, "o": outsider},
            )


async def share_document(brain: Brain, title: str) -> uuid.UUID:
    """Insert one document into the brain's group scope as the writer, return its id.

    brain: the seeded lattice.
    title: document title, doubling as its content hash for uniqueness.
    """
    document = uuid.uuid4()
    async with acting_as(brain.writer) as session:
        session.add(
            Document(
                id=document,
                content_hash=title,
                owner_id=brain.writer,
                scope=brain.group,
                title=title,
            )
        )
    return document


async def count_documents(
    principal: uuid.UUID, brain: Brain, scope: uuid.UUID | None = None
) -> int:
    """The brain's own documents a principal sees, optionally narrowed to one scope's graph.

    Counting only rows owned by the fixture's principals keeps the assertions hermetic when the
    shared dev database carries public groups from other runs, whose rows are visible to every
    caller by design and would otherwise inflate every count.

    principal: identity the read acts as.
    brain: the seeded lattice whose principals' documents are counted.
    scope: reading lens, the whole visible union when null.
    """
    owners = [brain.writer, brain.reader, brain.outsider]
    async with acting_as(principal, scope) as session:
        return (
            await session.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.__table__.c.owner_id.in_(owners))
            )
            or 0
        )


def test_reader_reads_the_shared_graph_but_cannot_write_it(requires_db: None) -> None:
    """A reader member sees the shared scope while every write path is refused or skipped.

    The insert fails on the write policy's WITH CHECK, and an update targeting the shared row
    matches zero rows since the UPDATE policy's USING filters it away, the silent-skip behavior
    background passes rely on.
    """

    async def probe() -> tuple[int, int]:
        async with shared_brain() as brain:
            await share_document(brain, "shared knowledge")
            seen = await count_documents(brain.reader, brain)
            with pytest.raises(ProgrammingError):
                async with acting_as(brain.reader) as session:
                    session.add(
                        Document(
                            content_hash="reader-write",
                            owner_id=brain.reader,
                            scope=brain.group,
                        )
                    )
            async with acting_as(brain.reader) as session:
                result = await session.execute(
                    text("UPDATE document SET title = 'defaced' WHERE scope = :g"),
                    {"g": brain.group},
                )
            return seen, result.rowcount

    seen, touched = asyncio.run(probe())
    assert seen == 1
    assert touched == 0


def test_public_group_reads_for_anyone_and_writes_for_members_only(requires_db: None) -> None:
    """Publishing a group opens reads to outsiders and the anonymous principal, never writes.

    Before publishing neither the outsider nor an anonymous session sees the shared row, after
    publishing both read it, and unpublishing hides it again. The outsider's attempted write into
    the public scope stays refused throughout, the read-only public contract.
    """

    async def probe() -> tuple[int, int, int, int, int]:
        async with shared_brain() as brain:
            await share_document(brain, "public knowledge")
            before_outsider = await count_documents(brain.outsider, brain)
            before_anon = await count_documents(settings.anonymous_principal_id, brain)
            await publish_group(brain.group)
            after_outsider = await count_documents(brain.outsider, brain)
            after_anon = await count_documents(settings.anonymous_principal_id, brain)
            with pytest.raises(ProgrammingError):
                async with acting_as(brain.outsider) as session:
                    session.add(
                        Document(
                            content_hash="outsider-write",
                            owner_id=brain.outsider,
                            scope=brain.group,
                        )
                    )
            await publish_group(brain.group, public=False)
            unpublished = await count_documents(brain.outsider, brain)
            return before_outsider, before_anon, after_outsider, after_anon, unpublished

    before_outsider, before_anon, after_outsider, after_anon, unpublished = asyncio.run(probe())
    assert before_outsider == 0 and before_anon == 0
    assert after_outsider == 1 and after_anon == 1
    assert unpublished == 0


def test_lens_narrows_reads_to_one_composed_graph(requires_db: None) -> None:
    """The reading lens projects exactly one group's graph out of the caller's visible union.

    A writer in two groups with a private row reads all three unlensed, exactly one under each
    group's lens, and the private row never rides along under any lens.
    """

    async def probe() -> tuple[int, int, int]:
        async with shared_brain() as brain:
            other = await create_group(f"brain-b-{uuid.uuid4().hex[:8]}")
            await add_member(brain.writer, other, role="writer")
            try:
                await share_document(brain, "in group A")
                async with acting_as(brain.writer) as session:
                    session.add(
                        Document(content_hash="in group B", owner_id=brain.writer, scope=other)
                    )
                    session.add(Document(content_hash="private", owner_id=brain.writer))
                union = await count_documents(brain.writer, brain)
                lens_a = await count_documents(brain.writer, brain, scope=brain.group)
                lens_b = await count_documents(brain.writer, brain, scope=other)
                return union, lens_a, lens_b
            finally:
                await delete_group(other)

    union, lens_a, lens_b = asyncio.run(probe())
    assert union == 3
    assert lens_a == 1
    assert lens_b == 1


def test_deleting_a_group_demotes_its_rows_to_private(requires_db: None) -> None:
    """Dropping a group nulls its rows' scope, so they fall back to their owners alone.

    The reader loses sight of the once-shared row while the writer, its owner, keeps it as a
    private document, the SET NULL demotion instead of a blocked or cascading delete.
    """

    async def probe() -> tuple[int, int, uuid.UUID | None]:
        async with shared_brain() as brain:
            document = await share_document(brain, "to be demoted")
            await delete_group(brain.group)
            reader_sees = await count_documents(brain.reader, brain)
            writer_sees = await count_documents(brain.writer, brain)
            async with acting_as(brain.writer) as session:
                scope = await session.scalar(select(Document.scope).where(Document.id == document))
            return reader_sees, writer_sees, scope

    reader_sees, writer_sees, scope = asyncio.run(probe())
    assert reader_sees == 0
    assert writer_sees == 1
    assert scope is None


def test_remove_member_revokes_visibility(requires_db: None) -> None:
    """A removed member stops seeing the shared scope while the group keeps its rows."""

    async def probe() -> tuple[int, int]:
        async with shared_brain() as brain:
            await share_document(brain, "before removal")
            before = await count_documents(brain.reader, brain)
            await remove_member(brain.reader, brain.group)
            return before, await count_documents(brain.reader, brain)

    before, after = asyncio.run(probe())
    assert before == 1
    assert after == 0


def test_promote_requires_a_writer_role(requires_db: None) -> None:
    """A reader may see the target scope yet cannot publish into it, the fail-fast writer check."""

    async def probe() -> None:
        async with shared_brain() as brain:
            document = uuid.uuid4()
            async with acting_as(brain.reader) as session:
                session.add(Document(id=document, content_hash="mine", owner_id=brain.reader))
            async with acting_as(brain.reader) as session:
                name = await session.scalar(
                    text("SELECT name FROM group_ WHERE id = :g"), {"g": brain.group}
                )
            assert name is not None
            with pytest.raises(ValueError, match="may not publish"):
                await promote(document, name, principal_id=brain.reader)

    asyncio.run(probe())
