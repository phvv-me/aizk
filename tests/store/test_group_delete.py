import uuid

import dbutil
import pytest
from sqlalchemy import text

from aizk.graph.ids import entity_id
from aizk.store import Group, system_session

pytestmark = pytest.mark.usefixtures("migrated_db")


async def document_scopes(doc_id: uuid.UUID) -> list[uuid.UUID]:
    """Read one document's raw scope set through the superuser connection, bypassing RLS."""
    async with dbutil.admin_engine().connect() as connection:
        row = await connection.execute(
            text("SELECT scopes FROM document WHERE id = :id"), {"id": doc_id}
        )
        return list(row.scalar_one())


def test_deleting_a_group_demotes_bridge_rows_fully_private() -> None:
    """A `{A, B}` row resets to `{}` when A is deleted, never silently narrowing to `{B}`."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_user(uuid.uuid4())
        group_a = await dbutil.seed_group(uuid.uuid4())
        group_b = await dbutil.seed_group(uuid.uuid4())
        bridge = await dbutil.seed_document(owner, [group_a, group_b])
        only_b = await dbutil.seed_document(owner, [group_b])

        async with system_session() as session:
            loaded = await session.get(Group, group_a)
            assert loaded is not None
            await loaded.delete(session)

        assert await document_scopes(bridge) == []
        assert await document_scopes(only_b) == [group_b]

    dbutil.run(body())


def test_group_deletion_cascades_memberships_and_removes_the_group() -> None:
    """Deleting a group drops its membership rows and the group itself."""

    async def body() -> None:
        await dbutil.reset_db()
        member = await dbutil.seed_user(uuid.uuid4())
        group_id = await dbutil.seed_group(uuid.uuid4())
        await dbutil.seed_membership(member, group_id, "writer")
        async with system_session() as session:
            loaded = await session.get(Group, group_id)
            assert loaded is not None
            await loaded.delete(session)
        async with dbutil.admin_engine().connect() as connection:
            groups = await connection.execute(
                text("SELECT count(*) FROM group_ WHERE id = :id"), {"id": group_id}
            )
            members = await connection.execute(
                text("SELECT count(*) FROM membership WHERE group_id = :id"), {"id": group_id}
            )
        assert groups.scalar_one() == 0
        assert members.scalar_one() == 0

    dbutil.run(body())


def test_demotion_dedupes_a_colliding_private_claim() -> None:
    """When an owner already privately claims a node, demoting its grouped claim drops the dupe.

    The grouped `entity_claim` and the owner's own private claim collide on
    `(content_id, owner_id, scopes='{}')` the moment the group is stripped, so the redundant
    grouped claim is deleted rather than left to violate the unique key.
    """

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_user(uuid.uuid4())
        group_id = await dbutil.seed_group(uuid.uuid4())
        content = entity_id("node", "Concept")
        await dbutil.admin_exec(
            "INSERT INTO entity_content (id, name, type) VALUES (:id, 'node', 'Concept')",
            {"id": content},
        )
        await dbutil.admin_exec(
            "INSERT INTO entity_claim (id, content_id, owner_id, scopes) "
            "VALUES (:id, :c, :o, '{}')",
            {"id": uuid.uuid4(), "c": content, "o": owner},
        )
        await dbutil.admin_exec(
            "INSERT INTO entity_claim (id, content_id, owner_id, scopes) "
            "VALUES (:id, :c, :o, CAST(:s AS uuid[]))",
            {"id": uuid.uuid4(), "c": content, "o": owner, "s": [str(group_id)]},
        )
        async with system_session() as session:
            loaded = await session.get(Group, group_id)
            assert loaded is not None
            await loaded.delete(session)
        async with dbutil.admin_engine().connect() as connection:
            remaining = await connection.execute(
                text("SELECT count(*) FROM entity_claim WHERE content_id = :c"), {"c": content}
            )
        assert remaining.scalar_one() == 1

    dbutil.run(body())
