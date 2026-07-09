import dbutil
import pytest
from fastmcp.server.auth.providers.introspection import IntrospectionTokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier

import aizk.mcp.user as user_mod
from aizk.config import settings
from aizk.mcp.user import cached_verifier, from_token, standing_from_claim, verifier
from aizk.store.identity import org_uuid, user_uuid


@pytest.mark.parametrize(
    ("issuer", "introspect", "expected"),
    [
        ("", "https://iss/introspect", type(None)),  # an empty issuer leaves auth off
        ("https://iss/a", "https://iss/introspect", IntrospectionTokenVerifier),  # live RFC 7662
        ("https://iss/b", "", JWTVerifier),  # no introspection url, the offline JWKS path
    ],
    ids=["auth-off", "introspection", "jwks"],
)
def test_cached_verifier_selects_the_verifier_class_from_the_oidc_settings(
    issuer: str, introspect: str, expected: type
) -> None:
    """cached_verifier builds the introspection or JWKS verifier, or none when the issuer is empty.

    The issuer values differ per case so `functools.cache` never returns another case's verifier,
    and no network is touched: the branch is asserted by the verifier class the settings select.
    """
    built = cached_verifier(
        issuer=issuer,
        jwks_uri="https://iss/jwks",
        introspect_url=introspect,
        client_id="cid",
        client_secret="secret",
        algorithm="ES384",
        required_scopes="",
        audience="",
    )
    assert isinstance(built, expected)


@pytest.mark.parametrize(
    ("issuer", "expected"),
    [("", type(None)), ("https://iss/live", JWTVerifier)],
    ids=["auth-off", "configured"],
)
def test_verifier_forwards_the_live_settings_to_cached_verifier(
    monkeypatch: pytest.MonkeyPatch, issuer: str, expected: type
) -> None:
    """verifier reads the currently configured OIDC settings, none when the issuer is empty."""
    monkeypatch.setattr(settings, "oidc_issuer", issuer)
    monkeypatch.setattr(settings, "oidc_jwks_url", "https://iss/jwks")
    monkeypatch.setattr(settings, "oidc_introspect_url", "")
    monkeypatch.setattr(settings, "oidc_client_id", "cid")
    monkeypatch.setattr(settings, "oidc_client_secret", "secret")
    assert isinstance(verifier(), expected)


class FakeVerifier:
    """A token verifier stand-in resolving a token to a fixed access token, no network.

    access_token: the object `verify_token` resolves to, null to drive the invalid-token branch.
    """

    def __init__(self, access_token: object) -> None:
        self.access_token = access_token

    async def verify_token(self, token: str) -> object:
        return self.access_token


def token_with(claims: dict[str, object]) -> object:
    """A verified access-token stand-in carrying a fixed `claims` mapping."""
    return type("Tok", (), {"claims": claims})()


@pytest.mark.parametrize(
    "active",
    [
        None,  # no verifier configured, an unauthenticated token resolves no one
        FakeVerifier(None),  # an invalid, unverifiable token
        FakeVerifier(token_with({})),  # verified but carries no `sub` claim
        FakeVerifier(token_with({"sub": 123})),  # a non-string subject
    ],
    ids=["no-verifier", "invalid", "no-subject", "non-string-subject"],
)
def test_from_token_resolves_no_one_when_the_token_never_yields_a_string_subject(
    monkeypatch: pytest.MonkeyPatch, active: object
) -> None:
    """from_token returns null for an unconfigured, invalid, or subject-less token."""
    monkeypatch.setattr(user_mod, "verifier", lambda: active)
    assert dbutil.run(from_token("tok")) is None


def test_from_token_derives_the_user_and_its_org_standing_from_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified token derives `uuid5(sub)` and reads its org standing off the claim."""
    claim = [
        {"id": "org_a", "name": "Alpha", "role": "editor"},  # writable
        {"id": "org_b", "name": "Beta", "role": "viewer"},  # read-only
    ]
    token = token_with({"sub": "logto|42", "aizk_groups": claim})
    monkeypatch.setattr(settings, "oidc_groups_claim", "aizk_groups")
    monkeypatch.setattr(user_mod, "verifier", lambda: FakeVerifier(token))

    user = dbutil.run(from_token("tok"))

    assert user is not None
    assert user.id == user_uuid("logto|42")
    assert set(user.orgs) == {org_uuid("org_a"), org_uuid("org_b")}
    assert user.writable_orgs == (org_uuid("org_a"),)  # only the editor role writes
    assert user.names == {"Alpha": org_uuid("org_a"), "Beta": org_uuid("org_b")}


@pytest.mark.parametrize(
    "role, writable",
    [("editor", True), ("admin", True), ("viewer", False), ("member", False)],
)
def test_standing_from_claim_writes_only_editor_and_admin_roles(role: str, writable: bool) -> None:
    """Editor and admin roles land in the writable subset, any other role only reads."""
    orgs, writers, names = standing_from_claim([{"id": "org_x", "name": "X", "role": role}])

    assert orgs == (org_uuid("org_x"),)
    assert names == {"X": org_uuid("org_x")}
    assert (writers == (org_uuid("org_x"),)) is writable


def test_standing_from_claim_skips_malformed_entries_without_crashing() -> None:
    """A malformed entry is logged and skipped while a valid one still resolves, never a crash."""
    claim = [
        {"id": "org_ok", "name": "Ok", "role": "editor"},  # valid
        {"name": "no-id"},  # missing id, skipped
        {"id": 123, "role": "viewer"},  # non-string id, skipped
        "not-a-mapping",  # not a mapping at all, skipped
    ]
    orgs, writers, names = standing_from_claim(claim)

    assert orgs == (org_uuid("org_ok"),)
    assert writers == (org_uuid("org_ok"),)
    assert names == {"Ok": org_uuid("org_ok")}


@pytest.mark.parametrize("claim", [None, "a string", 42], ids=["none", "string", "int"])
def test_standing_from_claim_reads_a_non_list_claim_as_empty_standing(claim: object) -> None:
    """A claim that is not a list of org dicts yields empty standing rather than raising."""
    assert standing_from_claim(claim) == ((), (), {})
