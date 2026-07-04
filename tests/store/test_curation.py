import uuid
from datetime import datetime

import dbutil
import pytest

from aizk.config import settings
from aizk.graph.ids import entity_id, fact_id
from aizk.store import Group, system_session

pytestmark = pytest.mark.usefixtures("migrated_db")


async def seed_fact(
    owner: uuid.UUID, scopes: list[uuid.UUID], statement: str, reviewed: bool
) -> uuid.UUID:
    """Seed one entity/content/claim triple and return the live claim id, superuser-inserted.

    owner: principal holding the claim.
    scopes: the claim's group set, empty for private.
    statement: the fact's self-contained text, also its content address.
    reviewed: whether the claim is stamped reviewed or left pending (reviewed_at null).
    """
    subject = entity_id("subject", "Concept")
    content = fact_id("subject", "related_to", "", statement)
    claim = uuid.uuid4()
    await dbutil.admin_exec(
        "INSERT INTO entity_content (id, name, type) VALUES (:id, 'subject', 'Concept') "
        "ON CONFLICT (id) DO NOTHING",
        {"id": subject},
    )
    await dbutil.admin_exec(
        "INSERT INTO fact_content (id, subject_id, predicate, statement) "
        "VALUES (:id, :subject, 'related_to', :statement) ON CONFLICT (id) DO NOTHING",
        {"id": content, "subject": subject, "statement": statement},
    )
    await dbutil.admin_exec(
        "INSERT INTO fact_claim (id, content_id, owner_id, scopes, reviewed_at) "
        "VALUES (:id, :content, :owner, CAST(:scopes AS uuid[]), "
        "CASE WHEN :reviewed THEN now() ELSE NULL END)",
        {
            "id": claim,
            "content": content,
            "owner": owner,
            "scopes": [str(s) for s in scopes],
            "reviewed": reviewed,
        },
    )
    return claim


@pytest.mark.parametrize(
    ("curated", "owner_is_admin", "stamped"),
    [(False, False, True), (True, False, False), (True, True, True)],
)
def test_review_stamp_gates_on_curated_admin_standing(
    curated: bool, owner_is_admin: bool, stamped: bool
) -> None:
    """A curated-group write stamps immediately only when the owner admins every such group."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_principal(uuid.uuid4())
        group = await dbutil.seed_group(uuid.uuid4(), curated=curated)
        await dbutil.seed_membership(owner, group, "admin" if owner_is_admin else "writer")
        async with system_session() as session:
            result = await Group.review_stamp(session, (group,), owner)
        assert (result is not None) is stamped
        if result is not None:
            assert isinstance(result, datetime)

    dbutil.run(body())


def test_review_stamp_private_write_is_immediate() -> None:
    """A private (empty scope) write always stamps immediately, the single-user path."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_principal(uuid.uuid4())
        async with system_session() as session:
            assert await Group.review_stamp(session, (), owner) is not None

    dbutil.run(body())


def test_pending_facts_lists_only_unreviewed_across_authors() -> None:
    """A curated group's queue shows every author's still-pending claim, newest-review-order."""

    async def body() -> None:
        await dbutil.reset_db()
        await dbutil.seed_principal(settings.system_principal_id, is_admin=True)
        author = await dbutil.seed_principal(uuid.uuid4())
        group = await dbutil.seed_group(uuid.uuid4(), curated=True)
        await seed_fact(author, [group], "pending one", reviewed=False)
        await seed_fact(author, [group], "approved already", reviewed=True)
        async with system_session() as session:
            loaded = await session.get(Group, group)
            assert loaded is not None
            pending = await loaded.pending_facts(session)
        assert [item.statement for item in pending] == ["pending one"]

    dbutil.run(body())


def test_approve_all_stamps_every_pending_claim() -> None:
    """`approve_facts` with no ids stamps every still-pending claim touching the group."""

    async def body() -> None:
        await dbutil.reset_db()
        await dbutil.seed_principal(settings.system_principal_id, is_admin=True)
        author = await dbutil.seed_principal(uuid.uuid4())
        group = await dbutil.seed_group(uuid.uuid4(), curated=True)
        await seed_fact(author, [group], "a", reviewed=False)
        await seed_fact(author, [group], "b", reviewed=False)
        async with system_session() as session:
            loaded = await session.get(Group, group)
            assert loaded is not None
            approved = await loaded.approve_facts(session)
            assert approved == 2
            assert await loaded.pending_facts(session) == []

    dbutil.run(body())


def test_approve_specific_ids_stamps_only_those_claims() -> None:
    """`approve_facts` with explicit ids stamps only the named pending claims, leaving the rest."""

    async def body() -> None:
        await dbutil.reset_db()
        await dbutil.seed_principal(settings.system_principal_id, is_admin=True)
        author = await dbutil.seed_principal(uuid.uuid4())
        group = await dbutil.seed_group(uuid.uuid4(), curated=True)
        chosen = await seed_fact(author, [group], "approve this", reviewed=False)
        await seed_fact(author, [group], "leave this", reviewed=False)
        async with system_session() as session:
            loaded = await session.get(Group, group)
            assert loaded is not None
            approved = await loaded.approve_facts(session, [chosen])
            assert approved == 1
            pending = await loaded.pending_facts(session)
        assert [item.statement for item in pending] == ["leave this"]

    dbutil.run(body())


def test_reject_deletes_named_pending_claims() -> None:
    """`reject_facts` deletes the named pending claims outright, they never become canonical."""

    async def body() -> None:
        await dbutil.reset_db()
        await dbutil.seed_principal(settings.system_principal_id, is_admin=True)
        author = await dbutil.seed_principal(uuid.uuid4())
        group = await dbutil.seed_group(uuid.uuid4(), curated=True)
        doomed = await seed_fact(author, [group], "reject me", reviewed=False)
        await seed_fact(author, [group], "keep me", reviewed=False)
        async with system_session() as session:
            loaded = await session.get(Group, group)
            assert loaded is not None
            removed = await loaded.reject_facts(session, [doomed])
            assert removed == 1
            remaining = await loaded.pending_facts(session)
        assert [item.statement for item in remaining] == ["keep me"]

    dbutil.run(body())
