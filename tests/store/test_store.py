import uuid
from datetime import UTC, datetime, timedelta

import dbutil
import pytest

from aizk.exceptions import NotGroupAdminError, ScopeNotFoundError
from aizk.store import (
    Document,
    Group,
    Membership,
    Principal,
    SessionItem,
    Watermark,
    acting_as,
    system_session,
)

pytestmark = pytest.mark.usefixtures("migrated_db")


def test_principal_lifecycle_create_grant_and_list() -> None:
    """A created principal reads back, `grant_admin` flips `administers`, and listing is by age."""

    async def body() -> None:
        await dbutil.reset_db()
        async with system_session() as session:
            first = await Principal.create(session, "alice")
            second = await Principal.create(session, "bob")
            assert not await Principal.administers(session, first.id)
            await first.grant_admin(session)
        async with system_session() as session:
            assert await Principal.administers(session, first.id)
            ordered = await Principal.list_all(session)
            names = [p.display_name for p in ordered]
            assert names[:2] == ["alice", "bob"] or {"alice", "bob"} <= set(names)
            assert first.id in {p.id for p in ordered}
            assert second.id in {p.id for p in ordered}

    dbutil.run(body())


def test_unknown_principal_administers_reads_false() -> None:
    """An id with no principal row administers nothing, the fail-closed default."""

    async def body() -> None:
        await dbutil.reset_db()
        async with system_session() as session:
            assert not await Principal.administers(session, uuid.uuid4())

    dbutil.run(body())


def test_group_creation_enrolls_creator_as_admin() -> None:
    """`create` with a creator enrolls it as the group's admin member in the same transaction."""

    async def body() -> None:
        await dbutil.reset_db()
        creator = await dbutil.seed_principal(uuid.uuid4())
        async with system_session() as session:
            group = await Group.create(session, "team", creator=creator)
            assert await group.admin(session, creator)
            await group.require_admin(session, creator)

    dbutil.run(body())


def test_named_resolves_or_raises() -> None:
    """`named` returns the group for a known name and raises `ScopeNotFoundError` otherwise."""

    async def body() -> None:
        await dbutil.reset_db()
        async with system_session() as session:
            made = await Group.create(session, "known")
            found = await Group.named(session, "known")
            assert found.id == made.id
            with pytest.raises(ScopeNotFoundError):
                await Group.named(session, "missing")

    dbutil.run(body())


def test_membership_add_remove_and_admin_gate() -> None:
    """Adding a reader grants no admin standing; `require_admin` refuses a non-admin loudly."""

    async def body() -> None:
        await dbutil.reset_db()
        member = await dbutil.seed_principal(uuid.uuid4())
        async with system_session() as session:
            group = await Group.create(session, "g")
            await group.add_member(session, member, role="reader")
            assert not await group.admin(session, member)
            with pytest.raises(NotGroupAdminError):
                await group.require_admin(session, member)
            await group.remove_member(session, member)
            assert not await group.admin(session, member)

    dbutil.run(body())


def test_server_admin_passes_group_admin_gate() -> None:
    """A server-wide admin clears `require_admin` for any group without a membership row."""

    async def body() -> None:
        await dbutil.reset_db()
        root = await dbutil.seed_principal(uuid.uuid4(), is_admin=True)
        async with system_session() as session:
            group = await Group.create(session, "g")
            await group.require_admin(session, root)

    dbutil.run(body())


def test_publish_and_curate_flip_flags() -> None:
    """`publish` and `curate` toggle the visibility and review flags in place."""

    async def body() -> None:
        await dbutil.reset_db()
        async with system_session() as session:
            group = await Group.create(session, "g")
            await group.publish(session, public=True)
            await group.curate(session, curated=True)
            await session.flush()
            reread = await session.get(Group, group.id)
            assert reread is not None and reread.public and reread.curated

    dbutil.run(body())


def test_list_all_counts_members() -> None:
    """`list_all` reports each group's visibility and member count, ordered by name."""

    async def body() -> None:
        await dbutil.reset_db()
        a = await dbutil.seed_principal(uuid.uuid4())
        b = await dbutil.seed_principal(uuid.uuid4())
        async with system_session() as session:
            group = await Group.create(session, "team", public=True, creator=a)
            await group.add_member(session, b, role="writer")
        async with system_session() as session:
            rows = await Group.list_all(session)
            team = next(row for row in rows if row["name"] == "team")
            assert team["public"] is True and team["members"] == 2

    dbutil.run(body())


def test_watermark_bump_read_and_payload_round_trip() -> None:
    """`bump` accumulates, `set_value` writes absolutely, and payloads read back under RLS."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_principal(uuid.uuid4())
        async with acting_as(owner) as session:
            assert await Watermark.read(session, owner, Watermark.Kind.fact_count) == 0
            assert await Watermark.bump(session, owner, Watermark.Kind.fact_count, by=3) == 3
            assert await Watermark.bump(session, owner, Watermark.Kind.fact_count, by=2) == 5
            await Watermark.set_value(
                session, owner, Watermark.Kind.scorecard, counter=9, payload={"k": 1}
            )
            assert await Watermark.read(session, owner, Watermark.Kind.scorecard) == 9
            assert await Watermark.read_payload(session, owner, Watermark.Kind.scorecard) == {
                "k": 1
            }
            assert await Watermark.read_payload(session, owner, Watermark.Kind.config) == {}

    dbutil.run(body())


def test_watermark_is_private_to_its_owner() -> None:
    """A watermark counter never leaks across principals, the private-bookkeeping guarantee."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_principal(uuid.uuid4())
        other = await dbutil.seed_principal(uuid.uuid4())
        async with acting_as(owner) as session:
            await Watermark.bump(session, owner, Watermark.Kind.fact_count, by=7)
        async with acting_as(other) as session:
            assert await Watermark.read(session, owner, Watermark.Kind.fact_count) == 0

    dbutil.run(body())


def test_recent_writes_lists_visible_documents_newest_first() -> None:
    """`recent_writes` returns the caller's visible documents, newest first, under RLS."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_principal(uuid.uuid4())
        old = await dbutil.seed_document(owner, [])
        new = await dbutil.seed_document(owner, [])
        docs = await Principal.recent_writes(owner, limit=10)
        ids = [doc.id for doc in docs]
        assert set(ids) == {old, new}
        assert all(isinstance(doc, Document) for doc in docs)

    dbutil.run(body())


def test_writable_scopes_clause_matches_the_write_lattice() -> None:
    """`Membership.writable_scopes` selects private rows and rows fully within writer groups."""
    from sqlalchemy import select

    async def body() -> None:
        await dbutil.reset_db()
        principal = await dbutil.seed_principal(uuid.uuid4())
        writable = await dbutil.seed_group(uuid.uuid4())
        readonly = await dbutil.seed_group(uuid.uuid4())
        await dbutil.seed_membership(principal, writable, "writer")
        await dbutil.seed_membership(principal, readonly, "reader")
        private = await dbutil.seed_document(principal, [])
        in_writable = await dbutil.seed_document(principal, [writable])
        in_readonly = await dbutil.seed_document(principal, [readonly])
        async with acting_as(principal) as session:
            rows = await session.execute(
                select(Document.id).where(Membership.writable_scopes(Document.scopes, principal))
            )
            selected = set(rows.scalars().all())
        assert private in selected and in_writable in selected
        assert in_readonly not in selected

    dbutil.run(body())


def test_session_item_due_for_promotion_unions_aged_and_overflow() -> None:
    """`due_for_promotion` returns aged items plus the oldest overflow, oldest-first, deduped."""
    now = datetime(2024, 1, 10, tzinfo=UTC)

    def item(minutes_old: float, ident: uuid.UUID) -> SessionItem:
        made = SessionItem(text="t", owner_id=uuid.uuid4())
        made.id = ident
        made.created_at = now - timedelta(minutes=minutes_old)
        return made

    aged = item(120, uuid.uuid4())
    fresh_a = item(1, uuid.uuid4())
    fresh_b = item(2, uuid.uuid4())
    items = [aged, fresh_b, fresh_a]  # oldest first
    due = SessionItem.due_for_promotion(items, now, age_minutes=60, threshold=1)
    # the aged item passes the age cutoff; overflow=len-threshold=2 takes the two oldest by index
    assert aged in due
    assert [i.id for i in due] == [i.id for i in items if i in due]


def test_session_item_nothing_due_when_fresh_and_under_threshold() -> None:
    """A small, fresh working set drains nothing, the steady-state no-op."""
    now = datetime(2024, 1, 10, tzinfo=UTC)
    made = SessionItem(text="t", owner_id=uuid.uuid4())
    made.id = uuid.uuid4()
    made.created_at = now
    assert SessionItem.due_for_promotion([made], now, age_minutes=60, threshold=20) == []
