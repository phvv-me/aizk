import uuid

import dbutil
import pytest
from fastmcp.server.auth.providers.introspection import IntrospectionTokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier

from aizk.config import settings
from aizk.store import User


@pytest.mark.parametrize(
    ("issuer", "introspect", "expected"),
    [
        ("", "https://iss/introspect", type(None)),  # an empty issuer leaves auth off
        ("https://iss/a", "https://iss/introspect", IntrospectionTokenVerifier),  # live RFC 7662
        ("https://iss/b", "", JWTVerifier),  # no introspection url, the offline JWKS path
    ],
    ids=["auth-off", "introspection", "jwks"],
)
def test_cached_verifier_selects_the_verifier_class_from_the_zitadel_settings(
    issuer: str, introspect: str, expected: type
) -> None:
    """cached_verifier builds the introspection or JWKS verifier, or none when the issuer is empty.

    The issuer values differ per case so `functools.cache` never returns another case's verifier,
    and no network is touched: the branch is asserted by the verifier class the settings select.
    """
    verifier = User.cached_verifier(
        issuer=issuer,
        jwks_uri="https://iss/jwks",
        introspect_url=introspect,
        client_id="cid",
        client_secret="secret",
        algorithm="ES384",
        required_scopes="",
    )
    assert isinstance(verifier, expected)


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
    assert isinstance(User.verifier(), expected)


class FakeVerifier:
    """A token verifier stand-in resolving a token to a fixed access token, no network.

    access_token: the object `verify_token` resolves to, null to drive the invalid-token branch.
    """

    def __init__(self, access_token: object) -> None:
        self.access_token = access_token

    async def verify_token(self, token: str) -> object:
        return self.access_token


@pytest.mark.parametrize(
    "verifier",
    [
        None,  # no verifier configured, an unauthenticated token resolves no one
        FakeVerifier(None),  # an invalid, unverifiable token
        FakeVerifier(type("Tok", (), {"claims": {}})()),  # verified but carries no `sub` claim
        FakeVerifier(type("Tok", (), {"claims": {"sub": 123}})()),  # a non-string subject
    ],
    ids=["no-verifier", "invalid", "no-subject", "non-string-subject"],
)
def test_from_token_resolves_no_one_when_the_token_never_yields_a_string_subject(
    monkeypatch: pytest.MonkeyPatch, verifier: object
) -> None:
    """from_token returns null for an unconfigured, invalid, or subject-less token.

    `for_subject` is stubbed to fail loudly so a wrongly-taken provisioning path would surface
    rather than silently minting a user on a token that should authenticate no one.
    """

    async def forbidden(subject: str) -> uuid.UUID:
        raise AssertionError("for_subject must not run on a subject-less token")

    monkeypatch.setattr(User, "verifier", classmethod(lambda cls: verifier))
    monkeypatch.setattr(User, "for_subject", classmethod(lambda cls, subject: forbidden(subject)))
    assert dbutil.run(User.from_token("tok")) is None


def test_from_token_maps_a_verified_subject_to_its_provisioned_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified token maps its `sub` claim to the user `for_subject` resolves for it."""
    resolved = uuid.uuid4()
    seen: dict[str, str] = {}

    async def stub_for_subject(subject: str) -> uuid.UUID:
        seen["subject"] = subject
        return resolved

    token = type("Tok", (), {"claims": {"sub": "zitadel|42"}})()
    monkeypatch.setattr(User, "verifier", classmethod(lambda cls: FakeVerifier(token)))
    monkeypatch.setattr(
        User, "for_subject", classmethod(lambda cls, subject: stub_for_subject(subject))
    )
    assert dbutil.run(User.from_token("tok")) == resolved
    assert seen == {"subject": "zitadel|42"}


def test_for_subject_provisions_on_first_sight_then_reuses_the_same_user(
    migrated_db: None,
) -> None:
    """for_subject mints a user for an unseen subject and returns the same one thereafter."""

    async def probe() -> None:
        await dbutil.reset_db()
        first = await User.for_subject("sub-A")
        assert await User.for_subject("sub-A") == first  # stable across calls
        assert await User.for_subject("sub-B") != first  # a new subject, a new user

    dbutil.run(probe())
