import asyncio
from dataclasses import dataclass

import dbutil
import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from id_factory import uuid5, uuid5s, uuid8
from pydantic import UUID5
from sqlalchemy.exc import DBAPIError
from sqlmodel import select

from aizk.config import settings
from aizk.store import Chunk, Document, Entity, NoTenantContext, TableBase
from aizk.store.engine import Database, Session
from aizk.store.identity import User

pytestmark = pytest.mark.usefixtures("migrated_db")


@dataclass
class Scenario:
    probe: UUID5
    other: UUID5
    member_orgs: set[UUID5]
    writer_orgs: set[UUID5]
    public_orgs: set[UUID5]
    owner_is_probe: bool
    scopes: list[UUID5]

    @property
    def creator(self) -> UUID5:
        return self.probe if self.owner_is_probe else self.other

    def expect_read(self) -> bool:
        row = set(self.scopes)
        readable = {self.probe, *self.member_orgs}
        standing = row <= readable or (len(row) == 1 and bool(row & self.public_orgs))
        return bool(row) and standing

    def expect_write(self) -> bool:
        row = set(self.scopes)
        return bool(row) and row <= {self.probe, *self.writer_orgs}


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    probe, other = draw(st.tuples(uuid5s, uuid5s).filter(lambda pair: pair[0] != pair[1]))
    org_pool = draw(st.lists(uuid5s, min_size=1, max_size=3, unique=True))
    member_orgs = {org for org in org_pool if draw(st.booleans())}
    writer_orgs = {org for org in member_orgs if draw(st.booleans())}
    public_orgs = {org for org in org_pool if draw(st.booleans())}
    scope_pool = [probe, *org_pool]
    scopes = draw(st.lists(st.sampled_from(scope_pool), min_size=1, max_size=3, unique=True))
    return Scenario(
        probe=probe,
        other=other,
        member_orgs=member_orgs,
        writer_orgs=writer_orgs,
        public_orgs=public_orgs,
        owner_is_probe=draw(st.booleans()),
        scopes=scopes,
    )


@given(scenario=scenarios())
@hyp_settings(max_examples=40, deadline=None)
def test_read_predicate_enforces_lattice(scenario: Scenario) -> None:
    async def body() -> None:
        await dbutil.reset_db()
        document = await dbutil.seed_document(scenario.creator, scenario.scopes)
        actual = await dbutil.can_read_document(
            scenario.probe,
            document,
            orgs=tuple(scenario.member_orgs),
            public_orgs=tuple(scenario.public_orgs),
        )
        assert actual == scenario.expect_read()

    dbutil.run(body())


@given(scenario=scenarios())
@hyp_settings(max_examples=40, deadline=None)
def test_write_predicate_enforces_lattice(scenario: Scenario) -> None:
    async def body() -> None:
        await dbutil.reset_db()
        actual = await dbutil.can_write_document(
            scenario.probe,
            scenario.creator,
            scenario.scopes,
            tuple(scenario.writer_orgs),
        )
        assert actual == scenario.expect_write()

    dbutil.run(body())


def test_creator_is_provenance_and_empty_scopes_are_rejected() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        actor, creator = uuid5(), uuid5()
        assert await dbutil.can_write_document(actor, creator, [actor])
        assert not await dbutil.can_write_document(actor, actor, [])

    dbutil.run(body())


def test_public_scope_grants_read_but_never_write() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        creator, caller, organization = uuid5(), uuid5(), uuid5()
        document = await dbutil.seed_document(creator, [organization])

        assert await dbutil.can_read_document(
            caller,
            document,
            public_orgs=(organization,),
        )
        assert not await dbutil.can_write_document(caller, caller, [organization])

    dbutil.run(body())


def test_read_authority_reaches_the_full_visible_union() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        user, org_a, org_b = uuid5(), uuid5(), uuid5()
        personal = await dbutil.seed_document(user, [user])
        in_a = await dbutil.seed_document(user, [org_a])
        in_b = await dbutil.seed_document(user, [org_b])
        candidates = [personal, in_a, in_b]
        standing = (org_a, org_b)

        assert await dbutil.visible_document_ids(user, candidates, standing) == set(candidates)

    dbutil.run(body())


def test_chunk_read_visibility_follows_its_document() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        creator, document_scope, unrelated_scope = (uuid5() for _ in range(3))
        document = Document(
            created_by=creator,
            scopes=[document_scope],
            content_hash=uuid8(),
        )
        chunk = Chunk(
            document_id=document.id,
            ord=0,
            text="document-owned chunk",
            created_by=creator,
            scopes=[unrelated_scope],
        )
        async with User.system().owner as database:
            database.add_all((document, chunk))

        async def visible(user: User) -> bool:
            async with user as database:
                return (
                    await database.scalar(select(Chunk.id).where(Chunk.id == chunk.id)) is not None
                )

        assert await visible(User.authorized(creator, read=(document_scope,)))
        assert not await visible(User.authorized(creator, read=(unrelated_scope,)))

    dbutil.run(body())


def test_chunk_write_requires_the_parent_document_scope() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        victim, attacker = uuid5(), uuid5()
        document_id = await dbutil.seed_document(victim, [victim])
        own_document_id = await dbutil.seed_document(attacker, [attacker])
        async with User.private(attacker) as database:
            database.add(
                Chunk(
                    document_id=own_document_id,
                    ord=0,
                    text="same-tenant child",
                    created_by=attacker,
                    scopes=[attacker],
                )
            )
        chunk = Chunk(
            document_id=document_id,
            ord=0,
            text="cross-tenant child",
            created_by=attacker,
            scopes=[attacker],
        )

        with pytest.raises(DBAPIError, match="row-level security"):
            async with User.private(attacker) as database:
                database.add(chunk)

    dbutil.run(body())


@pytest.mark.parametrize(
    ("row", "memberships", "public", "expected"),
    [
        (("a", "b"), ("a",), (), False),
        (("a", "b"), ("a", "b"), (), True),
        (("a",), (), ("a",), True),
        (("a", "b"), (), ("a",), False),
    ],
    ids=["partial-intersection", "full-intersection", "public-singleton", "public-bridge"],
)
def test_scope_intersections_and_public_access_follow_the_lattice(
    row: tuple[str, ...],
    memberships: tuple[str, ...],
    public: tuple[str, ...],
    expected: bool,
) -> None:
    async def body() -> None:
        await dbutil.reset_db()
        creator, reader, org_a, org_b = (uuid5() for _ in range(4))
        organizations = {"a": org_a, "b": org_b}
        document = await dbutil.seed_document(creator, [organizations[name] for name in row])
        actual = await dbutil.can_read_document(
            reader,
            document,
            orgs=tuple(organizations[name] for name in memberships),
            public_orgs=tuple(organizations[name] for name in public),
        )
        assert actual is expected

    dbutil.run(body())


@pytest.mark.parametrize(
    ("model", "error"),
    [(Document, NoTenantContext), (Entity.Kind, None)],
    ids=["scoped-refused", "unscoped-allowed"],
)
def test_tenant_context_is_required_only_for_scoped_models(
    model: type[TableBase], error: type[NoTenantContext] | None
) -> None:
    async def body() -> None:
        async with Session(Database.app().engine) as db, db.begin():
            if error is None:
                await db.exec(select(model).limit(1))
            else:
                with pytest.raises(error):
                    await db.exec(select(model).limit(1))

    dbutil.run(body())


def test_anonymous_session_reads_no_personal_row() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        creator = uuid5()
        personal = await dbutil.seed_document(creator, [creator])
        assert not await dbutil.can_read_document(settings.anonymous_user_id, personal)
        async with User.private(settings.anonymous_user_id) as database:
            assert database.user.id == settings.anonymous_user_id

    dbutil.run(body())


def test_one_user_can_own_overlapping_task_transactions() -> None:
    async def body() -> None:
        user = User.private(uuid5())
        first_open = asyncio.Event()
        second_open = asyncio.Event()
        first_closed = asyncio.Event()

        async def first() -> None:
            async with user as session:
                first_open.set()
                await second_open.wait()
                await session.exec(select(Entity.Kind).limit(1))
            first_closed.set()

        async def second() -> None:
            await first_open.wait()
            async with user as session:
                second_open.set()
                await first_closed.wait()
                await session.exec(select(Entity.Kind).limit(1))

        await asyncio.gather(first(), second())

    dbutil.run(body())
