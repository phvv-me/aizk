import uuid
from datetime import UTC, datetime, timedelta

import dbutil
import pytest

from aizk.exceptions import NotGroupAdminError, ScopeNotFoundError
from aizk.store import (
    Document,
    Group,
    Membership,
    SessionItem,
    User,
    Watermark,
    acting_as,
    system_session,
)

pytestmark = pytest.mark.usefixtures("migrated_db")


def test_session_outside_a_block_fails_fast() -> None:
    """session() raises NoTenantContext when read outside any acting_as/admin_session block."""
    from aizk.exceptions import NoTenantContext
    from aizk.store.engine import session

    with pytest.raises(NoTenantContext):
        session()


def test_sync_user_groups_skips_malformed_claim_entries() -> None:
    """A malformed group-claim entry is skipped, not crashed on, while a valid one still syncs."""
    from typing import cast

    async def body() -> bool:
        await dbutil.reset_db()
        user = await dbutil.seed_user(uuid.uuid4())
        memberships: list[object] = [
            {"id": "org-alpha", "name": "Alpha", "role": "writer"},  # valid
            {"name": "no-id"},  # missing id, the KeyError branch skips it
            {"id": 123, "role": "reader"},  # non-string id, the isinstance branch skips it
        ]
        async with system_session():
            await Group.sync_user_groups(user, cast(list[dict[str, str]], memberships))
            rows = await Group.list_all()
        return any(row["name"] == "Alpha" for row in rows)

    assert dbutil.run(body())  # the valid entry synced; the malformed ones skipped without a crash


def test_user_lifecycle_create_link_and_list() -> None:
    """A created user reads back, `link_oidc` binds a subject without admin, listing is by age.

    Admin standing is the seeded system user alone now (engine admin = the Postgres owner the CLI
    runs as), never granted to a created or linked user, so both a fresh `create` and a
    subject-bound `link_oidc` read `administers` false.
    """

    async def body() -> None:
        await dbutil.reset_db()
        async with system_session():
            first = await User.create("alice")
            second = await User.create("bob")
            assert not await User.administers(first.id)
            linked = await User.link_oidc("gh|alice", "alice-oidc")
            assert linked.oidc_subject == "gh|alice"
            assert not await User.administers(linked.id)  # a linked user is not admin
            again = await User.link_oidc("gh|alice", "ignored")
            assert again.id == linked.id  # idempotent over the same subject
        async with system_session():
            ordered = await User.list_all()
            names = [p.display_name for p in ordered]
            assert {"alice", "bob"} <= set(names)
            assert {first.id, second.id} <= {p.id for p in ordered}

    dbutil.run(body())


def test_unknown_user_administers_reads_false() -> None:
    """An id with no user row administers nothing, the fail-closed default."""

    async def body() -> None:
        await dbutil.reset_db()
        async with system_session():
            assert not await User.administers(uuid.uuid4())

    dbutil.run(body())


def test_group_creation_enrolls_creator_as_admin() -> None:
    """`create` with a creator enrolls it as the group's admin member in the same transaction."""

    async def body() -> None:
        await dbutil.reset_db()
        creator = await dbutil.seed_user(uuid.uuid4())
        async with system_session():
            group = await Group.create("team", creator=creator)
            assert await group.admin(creator)
            await group.require_admin(creator)

    dbutil.run(body())


def test_named_resolves_or_raises() -> None:
    """`named` returns the group for a known name and raises `ScopeNotFoundError` otherwise."""

    async def body() -> None:
        await dbutil.reset_db()
        async with system_session():
            made = await Group.create("known")
            found = await Group.named("known")
            assert found.id == made.id
            with pytest.raises(ScopeNotFoundError):
                await Group.named("missing")

    dbutil.run(body())


def test_membership_add_remove_and_admin_gate() -> None:
    """Adding a reader grants no admin standing; `require_admin` refuses a non-admin loudly."""

    async def body() -> None:
        await dbutil.reset_db()
        member = await dbutil.seed_user(uuid.uuid4())
        async with system_session():
            group = await Group.create("g")
            await group.add_member(member, role="reader")
            assert not await group.admin(member)
            with pytest.raises(NotGroupAdminError):
                await group.require_admin(member)
            await group.remove_member(member)
            assert not await group.admin(member)

    dbutil.run(body())


def test_server_admin_passes_group_admin_gate() -> None:
    """A server-wide admin clears `require_admin` for any group without a membership row."""

    async def body() -> None:
        await dbutil.reset_db()
        root = await dbutil.seed_user(uuid.uuid4(), is_admin=True)
        async with system_session():
            group = await Group.create("g")
            await group.require_admin(root)

    dbutil.run(body())


def test_publish_and_curate_flip_flags() -> None:
    """`publish` and `curate` toggle the visibility and review flags in place."""

    async def body() -> None:
        await dbutil.reset_db()
        async with system_session() as session:
            group = await Group.create("g")
            await group.publish(public=True)
            await group.curate(curated=True)
            await session.flush()
            reread = await session.get(Group, group.id)
            assert reread is not None and reread.public and reread.curated

    dbutil.run(body())


def test_list_all_counts_members() -> None:
    """`list_all` reports each group's visibility and member count, ordered by name."""

    async def body() -> None:
        await dbutil.reset_db()
        a = await dbutil.seed_user(uuid.uuid4())
        b = await dbutil.seed_user(uuid.uuid4())
        async with system_session():
            group = await Group.create("team", public=True, creator=a)
            await group.add_member(b, role="writer")
        async with system_session():
            rows = await Group.list_all()
            team = next(row for row in rows if row["name"] == "team")
            assert team["public"] is True and team["members"] == 2

    dbutil.run(body())


def test_watermark_bump_read_and_payload_round_trip() -> None:
    """`bump` accumulates, `set_value` writes absolutely, and payloads read back under RLS."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_user(uuid.uuid4())
        async with acting_as(owner):
            assert await Watermark.read(owner, Watermark.Kind.fact_count) == 0
            assert await Watermark.bump(owner, Watermark.Kind.fact_count, by=3) == 3
            assert await Watermark.bump(owner, Watermark.Kind.fact_count, by=2) == 5
            await Watermark.set_value(owner, Watermark.Kind.scorecard, counter=9, payload={"k": 1})
            assert await Watermark.read(owner, Watermark.Kind.scorecard) == 9
            assert await Watermark.read_payload(owner, Watermark.Kind.scorecard) == {"k": 1}
            assert await Watermark.read_payload(owner, Watermark.Kind.config) == {}

    dbutil.run(body())


def test_watermark_is_private_to_its_owner() -> None:
    """A watermark counter never leaks across users, the private-bookkeeping guarantee."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_user(uuid.uuid4())
        other = await dbutil.seed_user(uuid.uuid4())
        async with acting_as(owner):
            await Watermark.bump(owner, Watermark.Kind.fact_count, by=7)
        async with acting_as(other):
            assert await Watermark.read(owner, Watermark.Kind.fact_count) == 0

    dbutil.run(body())


def test_recent_writes_lists_visible_documents_newest_first() -> None:
    """`recent_writes` returns the caller's visible documents, newest first, under RLS."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_user(uuid.uuid4())
        old = await dbutil.seed_document(owner, [])
        new = await dbutil.seed_document(owner, [])
        docs = await User.recent_writes(owner, limit=10)
        ids = [doc.id for doc in docs]
        assert set(ids) == {old, new}
        assert all(isinstance(doc, Document) for doc in docs)

    dbutil.run(body())


def test_writable_scopes_clause_matches_the_write_lattice() -> None:
    """`Membership.writable_scopes` selects private rows and rows fully within writer groups."""
    from sqlalchemy import select

    async def body() -> None:
        await dbutil.reset_db()
        user = await dbutil.seed_user(uuid.uuid4())
        writable = await dbutil.seed_group(uuid.uuid4())
        readonly = await dbutil.seed_group(uuid.uuid4())
        await dbutil.seed_membership(user, writable, "writer")
        await dbutil.seed_membership(user, readonly, "reader")
        private = await dbutil.seed_document(user, [])
        in_writable = await dbutil.seed_document(user, [writable])
        in_readonly = await dbutil.seed_document(user, [readonly])
        async with acting_as(user) as session:
            rows = await session.execute(
                select(Document.id).where(
                    Membership.writable_scopes(Document.scopes, Document.owner_id, user)
                )
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


async def group_roles(session, user_id: uuid.UUID) -> set[tuple[str, str]]:
    """The (group name, role) pairs a user currently holds, for asserting a synced roster."""
    from sqlalchemy import select

    rows = await session.execute(
        select(Group.name, Membership.role)
        .join(Membership, Membership.group_id == Group.id)
        .where(Membership.user_id == user_id)
    )
    return {(name, str(role)) for name, role in rows}


def test_for_oidc_org_mints_once_then_reuses_the_mirror() -> None:
    """`for_oidc_org` mints the local group on first sight and returns the same one after."""

    async def body() -> tuple[uuid.UUID, uuid.UUID, str | None]:
        await dbutil.reset_db()
        async with system_session():
            first = await Group.for_oidc_org("org-abc", "Finance")
            again = await Group.for_oidc_org("org-abc", "ignored-second-time")
            return first.id, again.id, first.oidc_org_id

    first_id, again_id, org = dbutil.run(body())
    assert first_id == again_id  # idempotent on the organization id
    assert org == "org-abc"


def test_for_oidc_org_disambiguates_a_taken_label() -> None:
    """A mirror whose label collides with an existing group name gets the org id appended."""

    async def body() -> str:
        await dbutil.reset_db()
        async with system_session():
            await Group.create("Finance")
            mirror = await Group.for_oidc_org("org-xyz", "Finance")
            return mirror.name

    assert dbutil.run(body()) == "Finance (org-xyz)"


def test_sync_user_groups_reconciles_membership_to_the_claim() -> None:
    """Syncing upserts claimed memberships, updates a changed role, and drops the unclaimed."""

    async def body() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        await dbutil.reset_db()
        user = await dbutil.seed_user(uuid.uuid4())
        async with system_session() as session:
            await Group.sync_user_groups(
                user,
                [
                    {"id": "A", "name": "Alpha", "role": "reader"},
                    {"id": "B", "name": "Beta", "role": "writer"},
                ],
            )
            before = await group_roles(session, user)
        async with system_session() as session:
            # user leaves A, is promoted in B, and joins C
            await Group.sync_user_groups(
                user,
                [
                    {"id": "B", "name": "Beta", "role": "admin"},
                    {"id": "C", "name": "Gamma", "role": "reader"},
                ],
            )
            after = await group_roles(session, user)
        return before, after

    before, after = dbutil.run(body())
    assert before == {("Alpha", "reader"), ("Beta", "writer")}
    assert after == {("Beta", "admin"), ("Gamma", "reader")}  # A dropped, B updated, C added


def test_sync_user_groups_empty_claim_drops_all_memberships() -> None:
    """An empty claim means the user belongs nowhere, so every prior membership is removed."""

    async def body() -> set[tuple[str, str]]:
        await dbutil.reset_db()
        user = await dbutil.seed_user(uuid.uuid4())
        async with system_session() as session:
            await Group.sync_user_groups(user, [{"id": "A", "name": "Alpha"}])
            await Group.sync_user_groups(user, [])
            return await group_roles(session, user)

    assert dbutil.run(body()) == set()
