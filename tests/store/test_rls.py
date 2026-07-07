import uuid
from dataclasses import dataclass, field

import dbutil
import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from sqlalchemy import select

from aizk.config import settings
from aizk.store import Document, NoTenantContext, acting_as, async_session

pytestmark = pytest.mark.usefixtures("migrated_db")

ROLES = ("reader", "writer", "admin")
WRITER_ROLES = frozenset({"writer", "admin"})


@dataclass
class Scenario:
    """One RLS probe: a principal, a set of groups it may belong to, a scoped row, and a lens.

    groups: each group's id and its public flag.
    roles: the probe principal's membership role per group, absent when not a member.
    owner_is_probe: whether the row's owner is the probe principal or an unrelated third party.
    scopes: the group ids the row is shared with, empty for a private row.
    lens: the reading lens group ids, None for no lens at all.
    """

    groups: dict[uuid.UUID, bool]
    roles: dict[uuid.UUID, str]
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
    def member_groups(self) -> set[uuid.UUID]:
        """Every group the probe holds any role in, all of which grant read visibility."""
        return set(self.roles)

    @property
    def writer_groups(self) -> set[uuid.UUID]:
        """Every group the probe may write into, a writer or admin role."""
        return {gid for gid, role in self.roles.items() if role in WRITER_ROLES}

    @property
    def public_groups(self) -> set[uuid.UUID]:
        """Every public group, readable by anyone through the singleton branch."""
        return {gid for gid, is_public in self.groups.items() if is_public}

    def expect_read(self) -> bool:
        """The spec (not the code) for whether the probe reads the row, `ScopeLattice.read`."""
        scope_set = set(self.scopes)
        lens_ok = self.lens is None or (bool(scope_set) and scope_set <= set(self.lens))
        standing = (
            self.owner == self.probe
            or (bool(scope_set) and scope_set <= self.member_groups)
            or (len(scope_set) == 1 and next(iter(scope_set)) in self.public_groups)
        )
        return lens_ok and standing

    def expect_write_own(self) -> bool:
        """The spec for whether the probe may insert its own row with this scope set, `write`."""
        scope_set = set(self.scopes)
        return not scope_set or scope_set <= self.writer_groups


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    """A random principal/membership/scope-set/lens probe over one to three groups."""
    group_ids = draw(st.lists(st.uuids(version=4), min_size=1, max_size=3, unique=True))
    groups = {gid: draw(st.booleans()) for gid in group_ids}
    roles = {gid: draw(st.sampled_from(ROLES)) for gid in group_ids if draw(st.booleans())}
    scopes = draw(st.lists(st.sampled_from(group_ids), max_size=3, unique=True))
    lens_choice = draw(st.one_of(st.none(), st.lists(st.sampled_from(group_ids), unique=True)))
    # an empty lens tuple binds `app.scopes` to unset, exactly the no-lens case, so it collapses to
    # None rather than a set-but-empty lens the read spec would read as excluding every row.
    lens = None if not lens_choice else tuple(lens_choice)
    return Scenario(
        groups=groups,
        roles=roles,
        owner_is_probe=draw(st.booleans()),
        scopes=scopes,
        lens=lens,
    )


async def provision(scenario: Scenario) -> uuid.UUID:
    """Reset the schema and seed the scenario's principals, groups, memberships, and row."""
    await dbutil.reset_db()
    await dbutil.seed_user(scenario.probe)
    await dbutil.seed_user(scenario.other)
    for gid, is_public in scenario.groups.items():
        await dbutil.seed_group(gid, public=is_public)
    for gid, role in scenario.roles.items():
        await dbutil.seed_membership(scenario.probe, gid, role)
    return await dbutil.seed_document(scenario.owner, scenario.scopes)


@given(scenario=scenarios())
@hyp_settings(max_examples=40, deadline=None)
def test_read_predicate_enforces_lattice(scenario: Scenario) -> None:
    """The document read policy admits exactly the rows `ScopeLattice.read` says it should.

    One property covers the whole cross-product of ownership, membership standing, public shares,
    multi-group bridge sets, and the narrowing lens, so the security boundary is proven against its
    own spec rather than a handful of hand-picked examples.
    """

    async def body() -> None:
        doc_id = await provision(scenario)
        actual = await dbutil.can_read_document(scenario.probe, doc_id, scenario.lens or ())
        assert actual == scenario.expect_read()

    dbutil.run(body())


@given(scenario=scenarios())
@hyp_settings(max_examples=40, deadline=None)
def test_write_predicate_enforces_lattice(scenario: Scenario) -> None:
    """The document write-check policy admits exactly the inserts `ScopeLattice.write` allows.

    Visibility never implies write: a reader member and a public visitor read the shared graph but
    cannot insert into it, and a multi-group row needs writer standing in every group it names.
    """

    async def body() -> None:
        await dbutil.reset_db()
        await dbutil.seed_user(scenario.probe)
        for gid, is_public in scenario.groups.items():
            await dbutil.seed_group(gid, public=is_public)
        for gid, role in scenario.roles.items():
            await dbutil.seed_membership(scenario.probe, gid, role)
        actual = await dbutil.can_write_document(scenario.probe, scenario.probe, scenario.scopes)
        assert actual == scenario.expect_write_own()

    dbutil.run(body())


def test_missing_lens_reaches_full_union_and_lens_narrows() -> None:
    """A set lens narrows an already-visible read and excludes the owner's private layer.

    Anchors the lens semantics with a concrete two-group bridge: with no lens the owner sees its
    private row and both group rows; a lens of one group hides the private row and the other
    group's row, projecting exactly that group's slice.
    """

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_user(uuid.uuid4())
        group_a = await dbutil.seed_group(uuid.uuid4())
        group_b = await dbutil.seed_group(uuid.uuid4())
        await dbutil.seed_membership(owner, group_a, "writer")
        await dbutil.seed_membership(owner, group_b, "writer")
        private = await dbutil.seed_document(owner, [])
        in_a = await dbutil.seed_document(owner, [group_a])
        in_b = await dbutil.seed_document(owner, [group_b])
        candidates = [private, in_a, in_b]

        assert await dbutil.visible_document_ids(owner, candidates) == set(candidates)
        assert await dbutil.visible_document_ids(owner, candidates, (group_a,)) == {in_a}

    dbutil.run(body())


def test_public_group_reaches_non_member_only_as_singleton() -> None:
    """A public singleton row is readable by a non-member; a public+private bridge is not."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_user(uuid.uuid4())
        stranger = await dbutil.seed_user(uuid.uuid4())
        public = await dbutil.seed_group(uuid.uuid4(), public=True)
        private_group = await dbutil.seed_group(uuid.uuid4(), public=False)
        await dbutil.seed_membership(owner, public, "writer")
        await dbutil.seed_membership(owner, private_group, "writer")
        singleton = await dbutil.seed_document(owner, [public])
        bridge = await dbutil.seed_document(owner, [public, private_group])

        assert await dbutil.can_read_document(stranger, singleton)
        assert not await dbutil.can_read_document(stranger, bridge)

    dbutil.run(body())


def test_scoped_orm_query_without_acting_as_raises() -> None:
    """A scoped ORM read opened outside `acting_as` fails loud rather than returning nothing."""

    async def body() -> None:
        async with async_session()() as session, session.begin():
            with pytest.raises(NoTenantContext):
                await session.execute(select(Document))

    dbutil.run(body())


def test_non_scoped_query_without_acting_as_is_allowed() -> None:
    """A no-principal session may still read a non-scoped identity table past the tenant guard."""
    from aizk.store import User

    async def body() -> None:
        async with async_session()() as session, session.begin():
            # principal carries no row level security, so the tenant guard lets this pass
            await session.execute(select(User).limit(1))

    dbutil.run(body())


def test_anonymous_session_reads_no_private_row() -> None:
    """A session bound to the anonymous principal sees no private or member-only row."""

    async def body() -> None:
        await dbutil.reset_db()
        owner = await dbutil.seed_user(uuid.uuid4())
        private = await dbutil.seed_document(owner, [])
        assert not await dbutil.can_read_document(settings.anonymous_user_id, private)
        async with acting_as(settings.anonymous_user_id) as session:
            assert session.info["principal"] == settings.anonymous_user_id

    dbutil.run(body())
