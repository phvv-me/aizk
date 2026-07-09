import uuid
from dataclasses import dataclass, field

import dbutil
import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from sqlalchemy import select

from aizk.config import settings
from aizk.store import Document, EntityKind, NoTenantContext, acting_as, app_sessions
from aizk.store.identity import PUBLIC_ORG

pytestmark = pytest.mark.usefixtures("migrated_db")


@dataclass
class Scenario:
    """One RLS probe: a user, the orgs it stands in, a scoped row, and a read lens.

    There is no membership table any more, so a probe's standing is its org sets directly, exactly
    what `mcp.user.from_token` derives from a token and `caller_standing` binds. `bind_user` folds
    the reserved public org into every reader's orgs, so a row scoped to it alone is world-readable
    with no separate public branch.

    member_orgs: every org the probe belongs to, all of which grant read visibility.
    writer_orgs: the subset the probe may write into, editor-or-admin standing, `<= member_orgs`.
    owner_is_probe: whether the row's owner is the probe user or an unrelated third party.
    scopes: the org ids the row is shared with, empty for private, the public org for a share.
    lens: the reading lens org ids, None for no lens at all.
    """

    member_orgs: set[uuid.UUID]
    writer_orgs: set[uuid.UUID]
    owner_is_probe: bool
    scopes: list[uuid.UUID]
    lens: tuple[uuid.UUID, ...] | None
    probe: uuid.UUID = field(default_factory=uuid.uuid4)
    other: uuid.UUID = field(default_factory=uuid.uuid4)

    @property
    def owner(self) -> uuid.UUID:
        """The row owner, the probe itself or the unrelated third party."""
        return self.probe if self.owner_is_probe else self.other

    @property
    def read_orgs(self) -> set[uuid.UUID]:
        """Every org the read policy admits shares against, the public org folded in as `bind_user`
        folds it into every session.
        """
        return self.member_orgs | {PUBLIC_ORG}

    def expect_read(self) -> bool:
        """The spec (not the code) for whether the probe reads the row, `ScopeLattice.read`."""
        scope_set = set(self.scopes)
        lens_ok = self.lens is None or (bool(scope_set) and scope_set <= set(self.lens))
        standing = self.owner == self.probe or (bool(scope_set) and scope_set <= self.read_orgs)
        return lens_ok and standing

    def expect_write_own(self) -> bool:
        """The spec for whether the probe may insert its own row with this scope set, `write`.

        The public org is read-only to a member: it never enters `writer_orgs`, so publishing into
        public is an operator act, never a member write.
        """
        scope_set = set(self.scopes)
        return (not scope_set) or scope_set <= self.writer_orgs


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    """A random org-standing, scope-set, and lens probe over up to three orgs and public."""
    org_pool = draw(st.lists(st.uuids(version=4), min_size=1, max_size=3, unique=True))
    # sometimes admit the reserved public org into the pool so public shares are exercised, both
    # the readable singleton and the unreadable public-plus-private bridge
    if draw(st.booleans()):
        org_pool = [*org_pool, PUBLIC_ORG]
    member_orgs = {org for org in org_pool if draw(st.booleans())}
    writer_orgs = {org for org in member_orgs if draw(st.booleans())}
    scopes = draw(st.lists(st.sampled_from(org_pool), max_size=3, unique=True))
    lens_choice = draw(st.one_of(st.none(), st.lists(st.sampled_from(org_pool), unique=True)))
    # an empty lens tuple binds `app.scopes` to unset, exactly the no-lens case, so it collapses to
    # None rather than a set-but-empty lens the read spec would read as excluding every row.
    lens = None if not lens_choice else tuple(lens_choice)
    return Scenario(
        member_orgs=member_orgs,
        writer_orgs=writer_orgs,
        owner_is_probe=draw(st.booleans()),
        scopes=scopes,
        lens=lens,
    )


@given(scenario=scenarios())
@hyp_settings(max_examples=40, deadline=None)
def test_read_predicate_enforces_lattice(scenario: Scenario) -> None:
    """The document read policy admits exactly the rows `ScopeLattice.read` says it should.

    One property covers the whole cross-product of ownership, org standing, public shares,
    multi-org bridge sets, and the narrowing lens, so the security boundary is proven against its
    own spec rather than a handful of hand-picked examples.
    """

    async def body() -> None:
        await dbutil.reset_db()
        doc_id = await dbutil.seed_document(scenario.owner, scenario.scopes)
        actual = await dbutil.can_read_document(
            scenario.probe, doc_id, scenario.lens or (), tuple(scenario.member_orgs)
        )
        assert actual == scenario.expect_read()

    dbutil.run(body())


@given(scenario=scenarios())
@hyp_settings(max_examples=40, deadline=None)
def test_write_predicate_enforces_lattice(scenario: Scenario) -> None:
    """The document write-check policy admits exactly the inserts `ScopeLattice.write` allows.

    Visibility never implies write: a member and a public visitor read the shared graph but cannot
    insert into it, and a multi-org row needs editor standing in every org it names.
    """

    async def body() -> None:
        await dbutil.reset_db()
        actual = await dbutil.can_write_document(
            scenario.probe, scenario.probe, scenario.scopes, tuple(scenario.writer_orgs)
        )
        assert actual == scenario.expect_write_own()

    dbutil.run(body())


def test_write_check_rejects_a_private_row_owned_by_another_user() -> None:
    """An empty-scope insert is admitted only for the acting user's own private rows.

    A private row carries an empty scope set, and `'{}' <@ anything` is trivially true, so the
    write-check's containment branch alone would admit a forged private row under any victim's
    owner id. The policy gates the empty-scope branch on ownership, so an actor may write its own
    private row but never one owned by someone else.
    """

    async def body() -> None:
        await dbutil.reset_db()
        actor, victim = uuid.uuid4(), uuid.uuid4()
        assert await dbutil.can_write_document(actor, actor, []) is True
        assert await dbutil.can_write_document(actor, victim, []) is False

    dbutil.run(body())


def test_missing_lens_reaches_full_union_and_lens_narrows() -> None:
    """A set lens narrows an already-visible read and excludes the owner's private layer.

    Anchors the lens semantics with a concrete two-org bridge: with no lens the owner sees its
    private row and both org rows; a lens of one org hides the private row and the other org's row,
    projecting exactly that org's slice.
    """

    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        org_a, org_b = uuid.uuid4(), uuid.uuid4()
        standing = (org_a, org_b)
        private = await dbutil.seed_document(owner, [])
        in_a = await dbutil.seed_document(owner, [org_a])
        in_b = await dbutil.seed_document(owner, [org_b])
        candidates = [private, in_a, in_b]

        assert await dbutil.visible_document_ids(owner, candidates, (), standing) == set(
            candidates
        )
        assert await dbutil.visible_document_ids(owner, candidates, (org_a,), standing) == {in_a}

    dbutil.run(body())


def test_public_org_reaches_non_member_only_as_singleton() -> None:
    """A public singleton row is readable by a non-member; a public-plus-private bridge is not."""

    async def body() -> None:
        await dbutil.reset_db()
        owner, stranger = uuid.uuid4(), uuid.uuid4()
        private_org = uuid.uuid4()
        singleton = await dbutil.seed_document(owner, [PUBLIC_ORG])
        bridge = await dbutil.seed_document(owner, [PUBLIC_ORG, private_org])

        assert await dbutil.can_read_document(stranger, singleton)
        assert not await dbutil.can_read_document(stranger, bridge)

    dbutil.run(body())


def test_scoped_orm_query_without_acting_as_raises() -> None:
    """A scoped ORM read opened outside `acting_as` fails loud rather than returning nothing."""

    async def body() -> None:
        async with app_sessions()() as session, session.begin():
            with pytest.raises(NoTenantContext):
                await session.execute(select(Document))

    dbutil.run(body())


def test_non_scoped_query_without_acting_as_is_allowed() -> None:
    """A no-user session may still read a non-scoped table past the tenant guard."""

    async def body() -> None:
        async with app_sessions()() as session, session.begin():
            # the ontology carries no row level security, so the tenant guard lets this pass
            await session.execute(select(EntityKind).limit(1))

    dbutil.run(body())


def test_anonymous_session_reads_no_private_row() -> None:
    """A session bound to the anonymous user sees no private or member-only row."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = uuid.uuid4()
        private = await dbutil.seed_document(owner, [])
        assert not await dbutil.can_read_document(settings.anonymous_user_id, private)
        async with acting_as(settings.anonymous_user_id) as session:
            assert session.info["user"] == settings.anonymous_user_id

    dbutil.run(body())
