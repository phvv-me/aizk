import asyncio
import hashlib
from collections.abc import AsyncIterator
from typing import cast

import dbutil
import pytest
from id_factory import uuid5, uuid7
from pydantic import AnyHttpUrl, ValidationError
from starlette.requests import Request

from aizk.api.app import AizkAPI
from aizk.artifacts.models import ArtifactReceipt
from aizk.artifacts.service import ArtifactIntake
from aizk.artifacts.uploads import (
    InertIntake,
    UploadBox,
    UploadCapabilityError,
    UploadGrantLimitError,
    UploadRequest,
    UploadTicket,
    gather,
)
from aizk.config import Settings
from aizk.exceptions import ScopeNotFoundError
from aizk.integrations.docling import ArtifactBytes
from aizk.storage import ByteLimitExceeded
from aizk.store import Artifact
from aizk.store.identity import OrganizationStanding, User

pytestmark = pytest.mark.usefixtures("migrated_db")


def box(
    intake: ArtifactIntake | None = None,
    ttl_seconds: float = 60,
    live_grants_per_caller: int | None = None,
) -> UploadBox:
    """Build one capability store, with an inert intake unless a fake is given."""
    built = UploadBox(
        intake=cast("ArtifactIntake", intake if intake is not None else InertIntake()),
        ttl_seconds=ttl_seconds,
    )
    if live_grants_per_caller is None:
        return built
    return built.model_copy(update={"live_grants_per_caller": live_grants_per_caller})


def declared(size: int = 4, scopes: list[str] | None = None) -> UploadRequest:
    return UploadRequest(
        filename="paper.pdf",
        media_type="application/pdf",
        size=size,
        sha256=hashlib.sha256(b"data").hexdigest(),
        scopes=scopes,
        companion_text="Signed original",
    )


def test_upload_route_errors_are_never_cached() -> None:
    api = object.__new__(AizkAPI)
    request = Request({"type": "http", "method": "PUT", "path": "/api/uploads/missing"})

    response = dbutil.run(api.fail(request, UploadCapabilityError("missing")))

    assert response.status_code == 410
    assert response.headers["cache-control"] == "no-store"


def test_mint_issues_one_capability_claimable_exactly_once(settings: Settings) -> None:
    capabilities = box()
    organization = uuid5()
    user_id = uuid5()
    user = User.authorized(
        user_id,
        read=(user_id, organization),
        write=(user_id, organization),
        name="Pedro Valois",
        organizations=(
            OrganizationStanding(
                id=organization,
                name="Lab",
                roles=("editor",),
                permissions=("write:memory",),
            ),
        ),
    )

    grant = dbutil.run(capabilities.mint(user, declared(scopes=["Lab"])))

    origin = f"http://{settings.api_host}:{settings.api_port}"
    assert grant.url.startswith(f"{origin}/api/uploads/")
    assert grant.expires_seconds == 60
    capability = grant.url.rsplit("/", 1)[-1]
    ticket = dbutil.run(capabilities.claim(capability))
    assert ticket.user.id == user.id
    assert ticket.user.name == "Pedro Valois"
    assert ticket.user.scopes == user.scopes
    assert ticket.user.organizations == user.organizations
    assert ticket.user.write_scope(["Lab"]) == frozenset({organization})
    assert ticket.declared == declared(scopes=["Lab"])
    with pytest.raises(UploadCapabilityError, match="unknown or already used"):
        dbutil.run(capabilities.claim(capability))


def test_a_grant_minted_in_one_store_is_redeemed_by_an_independent_store() -> None:
    user = User.private(uuid5())

    grant = dbutil.run(box().mint(user, declared()))
    ticket = dbutil.run(box().claim(grant.url.rsplit("/", 1)[-1]))

    assert ticket.user.id == user.id
    assert ticket.declared == declared()


def test_mint_advertises_the_public_origin_when_configured(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setattr(settings, "api_public_url", AnyHttpUrl("https://api.aizk.test/"))

    grant = dbutil.run(box().mint(User.private(uuid5()), declared()))

    assert grant.url.startswith("https://api.aizk.test/api/uploads/")


def test_claims_reject_expired_capabilities_and_minting_prunes_them() -> None:
    user = User.private(uuid5())
    stale = dbutil.run(box(ttl_seconds=-1).mint(user, declared()))

    with pytest.raises(UploadCapabilityError, match="expired"):
        dbutil.run(box().claim(stale.url.rsplit("/", 1)[-1]))
    with pytest.raises(UploadCapabilityError, match="unknown or already used"):
        dbutil.run(box().claim("missing"))

    forgotten = dbutil.run(box(ttl_seconds=-1).mint(user, declared()))
    dbutil.run(box().mint(user, declared()))
    assert dbutil.run(dbutil.count_upload_grants(user.id)) == 1
    with pytest.raises(UploadCapabilityError, match="unknown or already used"):
        dbutil.run(box().claim(forgotten.url.rsplit("/", 1)[-1]))


def test_mint_holds_every_caller_to_the_live_grant_cap() -> None:
    capabilities = box(live_grants_per_caller=2)
    user, bystander = User.private(uuid5()), User.private(uuid5())

    first = dbutil.run(capabilities.mint(user, declared()))
    dbutil.run(capabilities.mint(user, declared()))
    with pytest.raises(UploadGrantLimitError, match="live upload grants"):
        dbutil.run(capabilities.mint(user, declared()))

    dbutil.run(capabilities.mint(bystander, declared()))  # the cap never crosses callers
    dbutil.run(capabilities.claim(first.url.rsplit("/", 1)[-1]))
    dbutil.run(capabilities.mint(user, declared()))
    assert dbutil.run(dbutil.count_upload_grants(user.id)) == 2


def test_concurrent_mints_atomically_hold_one_caller_to_the_live_grant_cap() -> None:
    capabilities = box(live_grants_per_caller=1)
    user = User.private(uuid5())

    async def race() -> tuple[object, ...]:
        results = await asyncio.gather(
            *(capabilities.mint(user, declared()) for _ in range(8)),
            return_exceptions=True,
        )
        return tuple(results)

    results = dbutil.run(race())

    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert sum(isinstance(result, UploadGrantLimitError) for result in results) == 7
    assert dbutil.run(dbutil.count_upload_grants(user.id)) == 1


def test_mint_requires_current_write_standing(settings: Settings) -> None:
    capabilities = box()
    stranger = User.private(uuid5())

    with pytest.raises(ScopeNotFoundError):
        dbutil.run(capabilities.mint(User.private(settings.anonymous_user_id), declared()))
    with pytest.raises(ScopeNotFoundError, match="no writable scope named 'Lab'"):
        dbutil.run(capabilities.mint(stranger, declared(scopes=["Lab"])))
    assert dbutil.run(dbutil.count_upload_grants(stranger.id)) == 0


def test_mint_rejects_declarations_over_its_configured_byte_limit(
    settings: Settings,
) -> None:
    config = settings.model_copy(update={"object_store_upload_byte_limit": 3})
    capabilities = UploadBox.from_settings(config, InertIntake())

    with pytest.raises(ValueError, match="less than or equal to 3"):
        dbutil.run(capabilities.mint(User.private(uuid5()), declared()))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("filename", "../evil.pdf", "safe path component"),
        ("filename", " ", "at least 1 character"),
        ("media_type", "application/\x00pdf", "unsafe character"),
        ("size", 0, "greater than 0"),
        ("scopes", [f"org-{index}" for index in range(33)], "at most 32 entries"),
        ("companion_text", "x" * 65_537, "at most 65536 characters"),
    ],
)
def test_upload_request_rejects_unsafe_declarations(
    field: str, value: str | int | list[str], message: str
) -> None:
    payload: dict[str, str | int | list[str]] = {
        "filename": "paper.pdf",
        "media_type": "application/pdf",
        "size": 4,
        "sha256": "0" * 64,
        field: value,
    }

    with pytest.raises(ValidationError, match=message):
        UploadRequest.model_validate(payload)


def test_gather_bounds_the_stream_by_the_declared_budget() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"ab"
        yield b"cd"

    assert dbutil.run(gather(chunks(), 4)) == b"abcd"
    with pytest.raises(ByteLimitExceeded, match="declared byte budget"):
        dbutil.run(gather(chunks(), 3))


def test_deliver_runs_the_claimed_upload_through_secure_intake() -> None:
    receipt = ArtifactReceipt(
        artifact_id=uuid7(),
        content_id=uuid7(),
        state=Artifact.Content.State.queued,
    )
    accepted: list[tuple[User, ArtifactBytes, list[str] | None, str | None]] = []

    class Intake:
        async def accept(
            self,
            user: User,
            artifact: ArtifactBytes,
            *,
            scopes: list[str] | None = None,
            companion_text: str | None = None,
        ) -> ArtifactReceipt:
            accepted.append((user, artifact, scopes, companion_text))
            return receipt

    user = User.private(uuid5())
    ticket = UploadTicket(user=user, declared=declared())

    assert dbutil.run(box(cast("ArtifactIntake", Intake())).deliver(ticket, b"data")) == receipt
    (caller, artifact, scopes, companion), *others = accepted
    assert others == []
    assert caller == user
    assert scopes is None
    assert companion == "Signed original"
    assert artifact.content == b"data"
    assert artifact.filename == "paper.pdf"
    assert artifact.media_type == "application/pdf"


def test_deliver_refuses_content_shorter_than_its_declaration() -> None:
    ticket = UploadTicket(user=User.private(uuid5()), declared=declared(size=4))

    with pytest.raises(ValueError, match="declared byte size"):
        dbutil.run(box().deliver(ticket, b"da"))


def test_inert_intake_refuses_to_deliver_anything() -> None:
    ticket = UploadTicket(user=User.private(uuid5()), declared=declared(size=4))

    with pytest.raises(RuntimeError, match="schema generation"):
        dbutil.run(box().deliver(ticket, b"data"))


def test_box_defaults_come_from_settings(settings: Settings) -> None:
    defaulted = UploadBox(intake=cast("ArtifactIntake", InertIntake()))

    assert defaulted.ttl_seconds == settings.api_upload_ttl_seconds
    assert defaulted.live_grants_per_caller == settings.api_upload_live_grants_per_caller
