import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastmcp.server.auth.providers.introspection import IntrospectionTokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from graphdb import (
    add_member,
    create_group,
    create_principal,
    grant_admin,
    group_admin,
    group_id_named,
    is_admin,
    list_groups,
    list_principals,
    recent_writes,
)
from sqlalchemy import bindparam, text

from aizk.auth import principal_for_token, tokens, verifier
from aizk.config import settings
from aizk.exceptions import ScopeNotFoundError
from aizk.store import (
    Document,
    Principal,
    acting_as,
    async_session,
)

# the issuer the test settings accept and the introspection endpoint the RFC 7662 path posts to,
# fed to `verifier()` through `override` so the seam selection is exercised against the real
# settings-to-verifier wiring rather than a mock of it
ISSUER = "https://issuer.test/aizk"
INTROSPECT_URL = "https://issuer.test/oauth/v2/introspect"

# global-settings override wired to the offline JWKS path, introspection left empty
JWT_OVERRIDE = {"zitadel_issuer": ISSUER, "zitadel_jwks_url": "https://issuer.test/jwks"}

# global-settings override wired to the introspection endpoint and client credentials, the path
# that flips `verifier()` from the offline JWKS check to the live introspection round-trip
INTROSPECT_OVERRIDE = {
    "zitadel_issuer": ISSUER,
    "zitadel_introspect_url": INTROSPECT_URL,
    "zitadel_client_id": "aizk-resource-server",
    "zitadel_client_secret": "introspection-secret",
}


class StubAccessToken:
    """A verified-token double carrying only the `claims` mapping `principal_for_token` reads.

    claims: the JWT-shaped claims a real `TokenVerifier.verify_token` would have returned.
    """

    def __init__(self, claims: dict[str, str]) -> None:
        self.claims = claims


class StubVerifier:
    """A `TokenVerifier` double whose `verify_token` resolves to a fixed claims dict or None.

    Stands in for a real `JWTVerifier` or `IntrospectionTokenVerifier`, both covered by fastmcp's
    own test suite, so aizk's tests exercise only the mapping from a verified token to a
    provisioned principal.

    claims: the claims a presented token resolves to, None to simulate a rejected token.
    """

    def __init__(self, claims: dict[str, str] | None) -> None:
        self.claims = claims

    async def verify_token(self, token: str) -> StubAccessToken | None:
        return None if self.claims is None else StubAccessToken(self.claims)


# raw teardown over the non-scoped identity tables, which carry no row level security, so a plain
# delete under any session reclaims the rows a DB test left behind even after a failed assertion


async def delete_principals(ids: list[uuid.UUID]) -> None:
    """Delete principals and their documents and memberships by id, the DB-test teardown.

    ids: principals whose dependent rows and own rows are removed.
    """
    for owner in ids:
        async with acting_as(owner) as session:
            await session.execute(
                text("DELETE FROM document WHERE owner_id = :owner"), {"owner": owner}
            )
    async with async_session()() as session, session.begin():
        await session.execute(
            text("DELETE FROM membership WHERE principal_id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": ids},
        )
        await session.execute(
            text("DELETE FROM principal WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": ids},
        )


async def delete_groups(ids: list[uuid.UUID]) -> None:
    """Delete groups by id once their memberships are gone, the group-test teardown.

    ids: groups to remove.
    """
    async with async_session()() as session, session.begin():
        await session.execute(
            text("DELETE FROM membership WHERE group_id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": ids},
        )
        await session.execute(
            text("DELETE FROM group_ WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": ids},
        )


# invariant (verifier selection): no issuer means auth is off, an issuer with no introspection url
# selects the offline JWTVerifier, and an issuer with one selects IntrospectionTokenVerifier, the
# one place the Zitadel settings choose which fastmcp verifier a bearer token is checked against


def test_verifier_returns_none_without_an_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no configured Zitadel issuer, auth is off and no verifier is built."""
    monkeypatch.setattr(settings, "zitadel_issuer", "")
    assert verifier() is None


def test_verifier_selects_jwt_or_introspection_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An introspection url selects IntrospectionTokenVerifier, its absence selects JWTVerifier."""
    for field, value in JWT_OVERRIDE.items():
        monkeypatch.setattr(settings, field, value)
    assert isinstance(verifier(), JWTVerifier)
    for field, value in INTROSPECT_OVERRIDE.items():
        monkeypatch.setattr(settings, field, value)
    assert isinstance(verifier(), IntrospectionTokenVerifier)


# DB invariant (admin roster): the roster lists every principal in created order, an unknown or a
# plain principal reads as not-admin, and grant_admin flips exactly the named principal


def test_admin_roster_lists_in_order_and_grant_flips_only_the_named_principal(
    requires_db: None,
) -> None:
    """list_principals is created-order, is_admin reads false for unknown and plain principals."""
    alice = asyncio.run(create_principal("alice"))
    bob = asyncio.run(create_principal("bob"))
    try:
        roster = asyncio.run(list_principals())
        stamps = [member.created_at for member in roster]
        assert stamps == sorted(stamps)
        assert {alice, bob} <= {member.id for member in roster}

        assert asyncio.run(is_admin(alice)) is False
        assert asyncio.run(is_admin(uuid.uuid4())) is False

        asyncio.run(grant_admin(alice))
        assert asyncio.run(is_admin(alice)) is True
        assert asyncio.run(is_admin(bob)) is False
    finally:
        asyncio.run(delete_principals([alice, bob]))


# DB invariant (group membership): add_member joins a principal to a group so the group surfaces
# through its association proxy, the scope path row level security later reads


def test_add_member_links_a_principal_to_a_group(requires_db: None) -> None:
    """create_group plus add_member makes the group reachable from the principal's memberships."""
    member = asyncio.run(create_principal("member"))
    group = asyncio.run(create_group("team"))
    try:
        asyncio.run(add_member(member, group))

        assert group in asyncio.run(group_ids_of(member))
    finally:
        asyncio.run(delete_groups([group]))
        asyncio.run(delete_principals([member]))


def test_create_group_enrolls_its_creator_as_admin_and_resolves_by_name(requires_db: None) -> None:
    """A founded group enrolls its creator as admin, resolves by name, and rejects an unknown one.

    Naming a creator joins them as the group's admin in the same transaction so they can review its
    canon at once, `group_id_named` round-trips the name back to that id, and an absent name fails
    fast rather than resolving to a silent private scope.
    """
    creator = asyncio.run(create_principal("founder"))
    name = f"team-{uuid.uuid4().hex[:8]}"
    try:
        group = asyncio.run(create_group(name, creator=creator))
        assert asyncio.run(group_admin(creator, group)) is True
        assert asyncio.run(group_id_named(name)) == group
        with pytest.raises(ScopeNotFoundError, match="no scope named"):
            asyncio.run(group_id_named(f"absent-{uuid.uuid4().hex[:8]}"))
    finally:
        asyncio.run(delete_groups([group]))
        asyncio.run(delete_principals([creator]))


def test_list_groups_reports_each_group_with_its_visibility_and_member_count(
    requires_db: None,
) -> None:
    """The roster carries a peopled and an ownerless group, counting members and none alike.

    A creator-founded public group surfaces once with a member count of one, the single admin
    enrollment `create_group` wrote, while an ownerless group the outer join leaves memberless
    reads back a count of zero, so the roster spans both the counted and the empty branch.
    """
    creator = asyncio.run(create_principal("lister"))
    peopled = f"roster-{uuid.uuid4().hex[:8]}"
    empty = f"empty-{uuid.uuid4().hex[:8]}"
    try:
        peopled_id = asyncio.run(create_group(peopled, public=True, creator=creator))
        empty_id = asyncio.run(create_group(empty))
        roster = {row["name"]: row for row in asyncio.run(list_groups())}
        assert roster[peopled] == {"name": peopled, "public": True, "members": 1}
        assert roster[empty] == {"name": empty, "public": False, "members": 0}
    finally:
        asyncio.run(delete_groups([peopled_id, empty_id]))
        asyncio.run(delete_principals([creator]))


async def group_ids_of(principal_id: uuid.UUID) -> list[uuid.UUID]:
    """The groups a principal belongs to, read through the membership association proxy.

    principal_id: principal whose group memberships are read.
    """
    async with acting_as(settings.system_principal_id) as session:
        principal = await session.get(Principal, principal_id)
        assert principal is not None
        await session.refresh(principal, ["memberships"])
        return [membership.group_id for membership in principal.memberships]


# DB invariant (audit listing): recent_writes returns the caller's own documents newest first and
# honors the limit, the visibility-scoped provenance read


def test_recent_writes_returns_owned_documents_newest_first_within_limit(
    requires_db: None,
) -> None:
    """recent_writes lists the principal's documents created-desc, truncated to the limit."""
    owner = asyncio.run(create_principal("writer"))
    try:
        asyncio.run(seed_documents(owner))

        listed = asyncio.run(recent_writes(owner, limit=2))

        assert [document.title for document in listed] == ["newest", "middle"]
    finally:
        asyncio.run(delete_principals([owner]))


async def seed_documents(owner: uuid.UUID) -> None:
    """Insert three documents for an owner with distinct created_at, oldest to newest.

    owner: principal the documents belong to, acting as itself so row security admits the write.
    """
    base = datetime(2024, 1, 1, tzinfo=UTC)
    async with acting_as(owner) as session:
        for offset, title in enumerate(("oldest", "middle", "newest")):
            session.add(
                Document(
                    owner_id=owner,
                    title=title,
                    content_hash=f"hash-{title}",
                    created_at=base + timedelta(hours=offset),
                )
            )


# DB invariant (subject provisioning): a Zitadel subject provisions one stable principal on first
# sight and resolves the same principal on every later lookup


def test_principal_for_subject_provisions_and_stays_stable(requires_db: None) -> None:
    """A subject provisions one principal on first sight, the same one on every later lookup."""
    subject = f"zitadel-{uuid.uuid4().hex}"
    provisioned = asyncio.run(Principal.for_subject(subject))
    try:
        assert asyncio.run(Principal.for_subject(subject)) == provisioned
    finally:
        asyncio.run(delete_principals([provisioned]))


# DB invariant (token provisioning): a token the verifier accepts maps its sub to one stable
# provisioned principal, while a rejected token or one missing a usable sub resolves to None and
# provisions no one, `principal_for_token`'s contract with a stubbed verifier standing in for the
# real JWT and introspection checks fastmcp's own suite covers


def test_principal_for_token_provisions_a_stable_principal(
    requires_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified token provisions one stable principal by its sub, unchanged on a repeat call."""
    subject = f"zitadel-{uuid.uuid4().hex}"
    monkeypatch.setattr(tokens, "verifier", lambda: StubVerifier({"sub": subject}))
    provisioned: uuid.UUID | None = None
    try:
        provisioned = asyncio.run(principal_for_token("any-token"))
        assert provisioned is not None
        assert asyncio.run(principal_for_token("any-token")) == provisioned
    finally:
        if provisioned is not None:
            asyncio.run(delete_principals([provisioned]))


def test_principal_for_token_resolves_none_for_a_rejected_or_subjectless_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected token, or one whose claims carry no string sub, never reaches provisioning.

    Spies on `Principal.for_subject` rather than a global principal count, so the assertion stays
    hermetic under any test order or DB state instead of depending on nothing else touching the
    principal table between the two calls.
    """

    async def unreachable(subject: str) -> uuid.UUID:
        raise AssertionError(f"Principal.for_subject reached with subject={subject!r}")

    monkeypatch.setattr(tokens.Principal, "for_subject", unreachable)

    monkeypatch.setattr(tokens, "verifier", lambda: StubVerifier(None))
    assert asyncio.run(principal_for_token("rejected")) is None

    monkeypatch.setattr(tokens, "verifier", lambda: StubVerifier({}))
    assert asyncio.run(principal_for_token("no-sub")) is None


def test_principal_for_token_resolves_none_when_auth_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no verifier configured, a presented token authenticates no one."""
    monkeypatch.setattr(settings, "zitadel_issuer", "")
    assert asyncio.run(principal_for_token("anything")) is None
