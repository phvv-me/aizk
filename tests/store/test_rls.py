import uuid
from dataclasses import dataclass

import dbutil
import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from sqlmodel import select

from aizk.config import settings
from aizk.store import Chunk, Document, EntityKind, NoTenantContext, TableBase
from aizk.store.engine import bypass_rls, session_factory
from aizk.store.identity import User

pytestmark = pytest.mark.usefixtures("migrated_db")


@dataclass
class Scenario:
    probe: uuid.UUID
    other: uuid.UUID
    member_orgs: set[uuid.UUID]
    writer_orgs: set[uuid.UUID]
    public_orgs: set[uuid.UUID]
    owner_is_probe: bool
    scopes: list[uuid.UUID]

    @property
    def creator(self) -> uuid.UUID:
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
    probe, other = draw(st.tuples(st.uuids(), st.uuids()).filter(lambda pair: pair[0] != pair[1]))
    org_pool = draw(st.lists(st.uuids(), min_size=1, max_size=3, unique=True))
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
        actor, creator = uuid.uuid4(), uuid.uuid4()
        assert await dbutil.can_write_document(actor, creator, [actor])
        assert not await dbutil.can_write_document(actor, actor, [])

    dbutil.run(body())


def test_read_authority_reaches_the_full_visible_union() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        user, org_a, org_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
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
        creator, document_scope, unrelated_scope = (uuid.uuid4() for _ in range(3))
        document = Document(
            created_by=creator,
            scopes=[document_scope],
            content_hash="read-through-document",
        )
        chunk = Chunk(
            document_id=document.id,
            ord=0,
            text="document-owned chunk",
            created_by=creator,
            scopes=[unrelated_scope],
        )
        async with bypass_rls() as database:
            database.add_all((document, chunk))

        async def visible(user: User) -> bool:
            async with user as database:
                return (
                    await database.scalar(select(Chunk.id).where(Chunk.id == chunk.id)) is not None
                )

        assert await visible(User.authorized(creator, read=(document_scope,)))
        assert not await visible(User.authorized(creator, read=(unrelated_scope,)))

    dbutil.run(body())


def test_intersection_requires_membership_in_every_scope() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        creator, reader, org_a, org_b = (uuid.uuid4() for _ in range(4))
        bridge = await dbutil.seed_document(creator, [org_a, org_b])
        assert not await dbutil.can_read_document(reader, bridge, orgs=(org_a,))
        assert await dbutil.can_read_document(reader, bridge, orgs=(org_a, org_b))

    dbutil.run(body())


def test_public_scope_reaches_non_member_only_as_singleton() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        creator, stranger, public_a, private_b = (uuid.uuid4() for _ in range(4))
        singleton = await dbutil.seed_document(creator, [public_a])
        bridge = await dbutil.seed_document(creator, [public_a, private_b])
        public = (public_a,)

        assert await dbutil.can_read_document(stranger, singleton, public_orgs=public)
        assert not await dbutil.can_read_document(stranger, bridge, public_orgs=public)

    dbutil.run(body())


@pytest.mark.parametrize(
    ("model", "error"),
    [(Document, NoTenantContext), (EntityKind, None)],
    ids=["scoped-refused", "unscoped-allowed"],
)
def test_tenant_context_is_required_only_for_scoped_models(
    model: type[TableBase], error: type[NoTenantContext] | None
) -> None:
    async def body() -> None:
        async with session_factory()() as db, db.begin():
            if error is None:
                await db.exec(select(model).limit(1))
            else:
                with pytest.raises(error):
                    await db.exec(select(model).limit(1))

    dbutil.run(body())


def test_anonymous_session_reads_no_personal_row() -> None:
    async def body() -> None:
        await dbutil.reset_db()
        creator = uuid.uuid4()
        personal = await dbutil.seed_document(creator, [creator])
        assert not await dbutil.can_read_document(settings.anonymous_user_id, personal)
        async with User.private(settings.anonymous_user_id) as database:
            assert database.user.id == settings.anonymous_user_id

    dbutil.run(body())
