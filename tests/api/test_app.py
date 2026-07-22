import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, time
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import dbutil
import httpx
import pytest
from id_factory import uuid5, uuid7
from obstore.exceptions import PermissionDeniedError
from patos import FrozenModel
from starlette.routing import Route

import aizk.api.app as app_module
from aizk.api.app import AizkAPI
from aizk.api.artifacts import ArtifactDashboard
from aizk.api.dashboard import Dashboard
from aizk.api.organizations import OrganizationDirectory, OrganizationView
from aizk.artifacts.models import ArtifactReceipt
from aizk.artifacts.service import ArtifactIntake
from aizk.artifacts.uploads import InertIntake, UploadBox, UploadRequest
from aizk.auth import Auth, Caller
from aizk.config import settings
from aizk.exceptions import ScopeNotFoundError
from aizk.integrations.clamav import MalwareRejectedError, MalwareUnavailableError
from aizk.integrations.docling import ArtifactBytes
from aizk.integrations.logto import LogtoClient, OrganizationChange
from aizk.status import (
    CallerStatus,
    ProcessingStatus,
    StatusReport,
    UsageReport,
    UsageStatus,
    UsageSummary,
)
from aizk.store import Artifact
from aizk.store.identity import OrganizationStanding, User
from aizk.types import Scopes

pytestmark = pytest.mark.usefixtures("migrated_db")

_DATA_SHA256 = hashlib.sha256(b"data").hexdigest()


class NestedBody(FrozenModel):
    """One nested schema used to verify local `$ref` inlining."""

    value: str


class ParentBody(FrozenModel):
    """One request schema containing a named nested model."""

    nested: NestedBody


def upload_request(size: int = 4) -> UploadRequest:
    """One declared four-byte original the MCP mint path would authorize."""
    return UploadRequest(
        filename="paper.pdf",
        media_type="application/pdf",
        size=size,
        sha256=_DATA_SHA256,
    )


# Every route except the capability PUT, whose single-use grant is its own authorization.
_PROTECTED = (
    ("GET", "/api/me", "/api/me"),
    ("GET", "/api/status", "/api/status"),
    ("GET", "/api/overview", "/api/overview"),
    ("GET", "/api/usage", "/api/usage"),
    ("GET", "/api/processing", "/api/processing"),
    ("GET", "/api/processing/events", "/api/processing/events"),
    ("GET", "/api/sources", "/api/sources"),
    ("GET", "/api/findings", "/api/findings"),
    ("GET", "/api/subjects", "/api/subjects"),
    ("GET", "/api/themes", "/api/themes"),
    ("GET", "/api/graph", "/api/graph"),
    ("POST", "/api/recall", "/api/recall"),
    ("GET", "/api/organizations", "/api/organizations"),
    ("POST", "/api/organizations", "/api/organizations"),
    ("POST", "/api/organizations/{name}/members", "/api/organizations/Lab/members"),
    (
        "PUT",
        "/api/organizations/{name}/members/{member_id}",
        "/api/organizations/Lab/members/member-1",
    ),
    (
        "DELETE",
        "/api/organizations/{name}/members/{member_id}",
        "/api/organizations/Lab/members/member-1",
    ),
)


def verified(user: User | None = None) -> Caller:
    """Build the caller a valid Logto bearer token would resolve to."""
    return Caller(subject="user-1", user=user or User.private(uuid5()))


class RecordingIntake:
    """Record accepted uploads and reply with one scripted receipt or failure."""

    def __init__(self, outcome: ArtifactReceipt | Exception) -> None:
        self.outcome = outcome
        self.accepted: list[ArtifactBytes] = []

    async def accept(
        self,
        user: User,
        artifact: ArtifactBytes,
        *,
        target: Scopes,
        companion_text: str | None = None,
    ) -> ArtifactReceipt:
        del user, target, companion_text
        self.accepted.append(artifact)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def box(
    intake: RecordingIntake | None = None,
    live_grants_per_caller: int | None = None,
) -> UploadBox:
    """Build one capability store over a recording or inert intake."""
    built = UploadBox(
        intake=cast("ArtifactIntake", intake if intake is not None else InertIntake()),
        ttl_seconds=60,
    )
    if live_grants_per_caller is None:
        return built
    return built.model_copy(update={"live_grants_per_caller": live_grants_per_caller})


def api(uploads: UploadBox | None = None) -> AizkAPI:
    """Build one API service over an inert memory intake."""
    return AizkAPI(
        Auth(),
        uploads if uploads is not None else box(),
        cast("ArtifactIntake", InertIntake()),
    )


def service_as(
    monkeypatch: pytest.MonkeyPatch, who: Caller | None, uploads: UploadBox | None = None
) -> AizkAPI:
    """Build one isolated API service whose bearer verification returns `who`."""
    built = api(uploads)
    monkeypatch.setattr(built.auth, "bearer", AsyncMock(return_value=who))
    return built


def call(
    service: AizkAPI,
    method: str,
    path: str,
    json: Mapping[str, str | int | list[str] | bool] | None = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Drive one request through the assembled ASGI application."""

    async def drive() -> httpx.Response:
        transport = httpx.ASGITransport(app=service.app())
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            return await client.request(method, path, json=json, content=content, headers=headers)

    return dbutil.run(drive())


@pytest.mark.parametrize(
    ("method", "path"),
    [(method, path) for method, _, path in _PROTECTED],
)
def test_every_authenticated_route_rejects_an_unverified_bearer(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    response = call(service_as(monkeypatch, None), method, path)

    assert response.status_code == 401
    assert response.json() == {"detail": "a valid Logto bearer token is required"}


def test_the_protected_route_list_covers_every_registered_surface() -> None:
    routes = api().app().routes
    exposed = {
        (method, route.path)
        for route in routes
        if isinstance(route, Route) and route.methods is not None
        for method in route.methods - {"HEAD", "OPTIONS"}
    }

    assert exposed == {(method, template) for method, template, _ in _PROTECTED} | {
        ("PUT", "/api/uploads/{capability}"),
        ("GET", "/healthz"),
    }


def test_health_check_is_cheap_and_unauthenticated() -> None:
    response = call(api(), "GET", "/healthz")

    assert response.status_code == 204
    assert response.content == b""


def test_json_body_inlines_nested_model_definitions() -> None:
    body = app_module.json_body(ParentBody)
    schema = body["requestBody"]["content"]["application/json"]["schema"]

    assert "$defs" not in str(schema)
    assert schema["properties"]["nested"]["properties"]["value"]["type"] == "string"


def test_bearer_verification_receives_only_bearer_scheme_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bearer = AsyncMock(return_value=None)
    service = api()
    monkeypatch.setattr(service.auth, "bearer", bearer)

    first = call(service, "GET", "/api/me", headers={"Authorization": "Bearer abc"})
    second = call(service, "GET", "/api/overview", headers={"Authorization": "Basic abc"})
    third = call(service, "POST", "/api/recall")

    assert (first.status_code, second.status_code, third.status_code) == (401, 401, 401)
    assert [request.args for request in bearer.await_args_list] == [("abc",), ("",), ("",)]


def test_me_returns_the_label_and_exact_organization_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, shared = uuid5(), uuid5()
    user = User.authorized(
        owner,
        read=(owner, shared),
        write=(owner, shared),
        name="Pedro Valois",
        organizations=(
            OrganizationStanding(
                id=shared,
                name="Lab",
                description="Shared experiments",
                roles=("editor",),
                permissions=("write:memory",),
            ),
        ),
    )

    response = call(service_as(monkeypatch, verified(user)), "GET", "/api/me")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "label": "Pedro Valois",
        "organizations": [
            {
                "name": "Lab",
                "description": "Shared experiments",
                "roles": ["editor"],
                "permissions": ["write:memory"],
                "writable": True,
                "public": False,
            }
        ],
    }


def test_status_returns_combined_caller_usage_and_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    who = verified()
    now = datetime(2026, 7, 20, tzinfo=UTC)
    report = StatusReport(
        generated_at=now,
        caller=CallerStatus.from_user(who.user),
        usage=UsageStatus.from_report(
            UsageReport(
                generated_at=now,
                recorded_through=now,
                days=7,
                start=datetime.combine(now.date(), time.min, tzinfo=UTC),
                summary=UsageSummary(requests=2),
                lifetime=UsageSummary(requests=5),
            )
        ),
        processing=ProcessingStatus(generated_at=now, state="idle", stages=()),
    )
    load = AsyncMock(return_value=report)
    monkeypatch.setattr(app_module.StatusReport, "load", load)
    service = service_as(monkeypatch, who)

    response = call(service, "GET", "/api/status?days=7")
    invalid = call(service, "GET", "/api/status?days=0")

    assert response.status_code == 200
    assert response.json()["caller"]["anonymous"] is False
    assert response.json()["usage"]["lifetime"]["requests"] == 5
    assert response.json()["processing"]["state"] == "idle"
    assert invalid.status_code == 400
    load.assert_awaited_once_with(who.user, 7)


def test_overview_merges_knowledge_usage_sources_and_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    who = verified()
    dashboard = AsyncMock(return_value=Dashboard())
    artifacts = AsyncMock(return_value=ArtifactDashboard())
    monkeypatch.setattr(app_module.Dashboard, "load", dashboard)
    monkeypatch.setattr(app_module.ArtifactDashboard, "load", artifacts)

    response = call(service_as(monkeypatch, who), "GET", "/api/overview")

    assert response.status_code == 200, response.text
    assert set(response.json()) == {"totals", "usage", "recent_documents", "artifacts"}
    assert dashboard.await_args is not None and dashboard.await_args.args == (who.user,)
    assert artifacts.await_args is not None and artifacts.await_args.args == (who.user,)


@pytest.mark.parametrize(
    "path",
    [
        "/api/usage?days=30",
        "/api/processing",
        "/api/sources",
        "/api/findings",
        "/api/subjects",
        "/api/themes",
        "/api/graph",
    ],
)
def test_read_only_analysis_routes_execute_against_postgres(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    response = call(service_as(monkeypatch, verified()), "GET", path)

    assert response.status_code == 200, response.text


def test_processing_events_stream_authenticated_snapshots_without_buffering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    who = verified()
    seen: dict[str, User] = {}

    class FiniteProcessingUpdates:
        def __init__(
            self,
            user: User,
            disconnected: Callable[[], Awaitable[bool]],
        ) -> None:
            del disconnected
            seen["user"] = user

        async def events(self) -> AsyncIterator[bytes]:
            yield b"event: processing\ndata: {}\n\n"

    monkeypatch.setattr(app_module, "ProcessingUpdates", FiniteProcessingUpdates)

    response = call(
        service_as(monkeypatch, who),
        "GET",
        "/api/processing/events",
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.content == b"event: processing\ndata: {}\n\n"
    assert seen == {"user": who.user}


@pytest.mark.parametrize(
    ("path", "detail"),
    [
        ("/api/usage?days=0", "days must be between 1 and 365"),
        ("/api/usage?days=366", "days must be between 1 and 365"),
        ("/api/sources?limit=0", "limit must be between 1 and 100"),
        ("/api/sources?limit=101", "limit must be between 1 and 100"),
        ("/api/sources?offset=-1", "offset must be nonnegative"),
        ("/api/graph?limit=0", "limit must be between 1 and 80"),
        ("/api/graph?limit=81", "limit must be between 1 and 80"),
    ],
)
def test_read_only_analysis_routes_reject_out_of_bounds_queries(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    detail: str,
) -> None:
    response = call(service_as(monkeypatch, verified()), "GET", path)

    assert response.status_code == 400
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize("resource", ["sources", "findings", "subjects"])
def test_catalog_routes_execute_the_optional_search(
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
) -> None:
    response = call(
        service_as(monkeypatch, verified()),
        "GET",
        f"/api/{resource}?search=needle",
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("budget", [512, None], ids=["explicit", "default"])
def test_recall_forwards_the_query_budget_and_caller(
    monkeypatch: pytest.MonkeyPatch, budget: int | None
) -> None:
    who = verified()
    seen: dict[str, User | str | int] = {}

    class FakeMemory:
        def __init__(self, *, user: User, intake: ArtifactIntake) -> None:
            del intake
            seen["user"] = user

        async def recall(self, query: str, budget: int) -> SimpleNamespace:
            seen["query"], seen["budget"] = query, budget

            async def to_markdown() -> str:
                return "## Evidence"

            return SimpleNamespace(to_markdown=to_markdown)

    monkeypatch.setattr(app_module, "Memory", FakeMemory)
    body: dict[str, str | int] = {"query": "  what holds  "}
    if budget is not None:
        body["budget"] = budget

    response = call(service_as(monkeypatch, who), "POST", "/api/recall", json=body)

    assert response.status_code == 200
    assert response.json() == {"markdown": "## Evidence"}
    assert seen == {
        "user": who.user,
        "query": "what holds",
        "budget": budget or settings.context_token_budget,
    }


@pytest.mark.parametrize(
    "content",
    [b"not json", b'{"query": ""}'],
    ids=["malformed", "blank-query"],
)
def test_invalid_json_bodies_are_unprocessable(
    monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    response = call(service_as(monkeypatch, verified()), "POST", "/api/recall", content=content)

    assert response.status_code == 422
    assert "detail" in response.json()


@pytest.mark.parametrize(
    ("failure", "status", "detail"),
    [
        (
            ValueError("remember requires text or a source URI"),
            400,
            "remember requires text or a source URI",
        ),
        (
            ScopeNotFoundError("no writable scope named 'Lab'"),
            403,
            "no writable scope named 'Lab'",
        ),
        (
            PermissionError("organization administration is not permitted"),
            403,
            "organization administration is not permitted",
        ),
        (
            MalwareRejectedError("Win.Test.EICAR_HDB-1 found in stream"),
            422,
            "the source was rejected by the safety scan",
        ),
        (
            MalwareUnavailableError("clamd refused the connection at clamav:3310"),
            503,
            "safety scanning is temporarily unavailable",
        ),
        (
            PermissionDeniedError("Generic S3 error: ... 403 Forbidden"),
            503,
            "object storage is temporarily unavailable",
        ),
        (
            httpx.ConnectError("[Errno -2] Name or service not known"),
            502,
            "an upstream request could not be completed",
        ),
    ],
)
def test_expected_domain_failures_map_to_stable_sanitized_details(
    failure: Exception, status: int, detail: str
) -> None:
    assert AizkAPI.status_for(failure) == status
    assert AizkAPI.detail_for(failure) == detail


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"query": "x" * 20}, "query"),
        ({"query": "what", "budget": 10_000_000}, "budget"),
    ],
)
def test_request_models_enforce_the_deployment_bounds_at_validation_time(
    monkeypatch: pytest.MonkeyPatch, body: dict, field: str
) -> None:
    monkeypatch.setattr(settings, "mcp_recall_query_max_chars", 10)

    response = call(service_as(monkeypatch, verified()), "POST", "/api/recall", json=body)

    assert response.status_code == 422
    assert field in response.json()["detail"]


def test_status_mapping_rejects_an_unexpected_failure_family() -> None:
    with pytest.raises(TypeError, match="unsupported API failure"):
        AizkAPI.status_for(RuntimeError("not a domain failure"))


def test_upload_capability_minted_by_the_mint_path_is_redeemed_by_the_put_without_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = ArtifactReceipt(
        artifact_id=uuid7(),
        content_id=uuid7(),
        state=Artifact.Content.State.queued,
    )
    intake = RecordingIntake(receipt)
    who = verified()
    minting = api()
    # An independent store on the receiving service proves the grant crosses processes
    # through PostgreSQL rather than through shared interpreter state.
    receiving = api(box(intake))
    monkeypatch.setattr(receiving.auth, "bearer", AsyncMock(return_value=None))

    grant = dbutil.run(minting.uploads.mint(who.user, upload_request()))
    path = httpx.URL(grant.url).path

    first = call(receiving, "PUT", path, content=b"data")
    replay = call(receiving, "PUT", path, content=b"data")

    assert first.status_code == 200
    assert first.json() == {
        "artifact_id": str(receipt.artifact_id),
        "content_id": str(receipt.content_id),
        "state": "queued",
    }
    assert [artifact.content for artifact in intake.accepted] == [b"data"]
    assert replay.status_code == 410


def test_upload_put_enforces_the_declared_byte_budget_and_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intake = RecordingIntake(MalwareRejectedError("infected"))
    who = verified()
    service = service_as(monkeypatch, who, uploads=box(intake))

    def minted_path() -> str:
        grant = dbutil.run(service.uploads.mint(who.user, upload_request()))
        return httpx.URL(grant.url).path

    oversize = call(service, "PUT", minted_path(), content=b"12345")
    short = call(service, "PUT", minted_path(), content=b"da")
    unknown = call(service, "PUT", "/api/uploads/unknown", content=b"data")
    infected = call(service, "PUT", minted_path(), content=b"data")

    assert oversize.status_code == 413
    assert short.status_code == 400
    assert short.json() == {"detail": "the upload does not match its declared byte size"}
    assert unknown.status_code == 410
    assert infected.status_code == 422
    assert infected.json() == {"detail": "the source was rejected by the safety scan"}
    assert [artifact.content for artifact in intake.accepted] == [b"data"]


def test_upload_put_maps_an_object_store_outage_to_a_stable_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intake = RecordingIntake(PermissionDeniedError("Generic S3 error: ... 403 Forbidden"))
    who = verified()
    service = service_as(monkeypatch, who, uploads=box(intake))
    grant = dbutil.run(service.uploads.mint(who.user, upload_request()))
    path = httpx.URL(grant.url).path

    response = call(service, "PUT", path, content=b"data")

    assert response.status_code == 503
    assert response.json() == {"detail": "object storage is temporarily unavailable"}


def test_json_routes_refuse_bodies_past_the_api_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mcp_remember_max_chars", 2)  # budget becomes 16 bytes
    service = service_as(monkeypatch, verified())

    declared = call(service, "POST", "/api/recall", content=b"x" * 32)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"x" * 12
        yield b"x" * 12

    async def stream_without_length() -> httpx.Response:
        transport = httpx.ASGITransport(app=service.app())
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            return await client.post("/api/recall", content=chunks())

    streamed = dbutil.run(stream_without_length())

    assert declared.status_code == 413
    assert declared.json() == {"detail": "the request body exceeds the API byte budget"}
    assert streamed.status_code == 413
    assert streamed.json() == {"detail": "the upload exceeds its declared byte budget"}


def test_organizations_directory_loads_for_the_verified_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = OrganizationDirectory(
        organizations=(OrganizationView(name="Lab", description="Shared experiments"),)
    )
    load = AsyncMock(return_value=directory)
    monkeypatch.setattr(app_module.OrganizationDirectory, "load", load)
    service = service_as(monkeypatch, verified())

    response = call(service, "GET", "/api/organizations")

    assert response.status_code == 200
    assert response.json() == directory.model_dump(mode="json")
    assert load.await_args is not None
    assert load.await_args.args == (service.auth.client, "user-1")


def test_organization_mutations_run_through_the_authorized_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    who = verified()
    built: list[tuple[User, str]] = []
    actions: list[tuple[str, ...]] = []

    class FakeManager:
        def __init__(self, client: LogtoClient, user: User, subject: str) -> None:
            del client
            built.append((user, subject))

        async def create(self, name: str, description: str | None) -> OrganizationChange:
            actions.append(("create", name, description or ""))
            return OrganizationChange(organization=name)

        async def add(self, name: str, email: str, role: str) -> OrganizationChange:
            actions.append(("add", name, email, role))
            return OrganizationChange(organization=name, member=email)

        async def set_role(self, name: str, member: str, role: str) -> OrganizationChange:
            actions.append(("set_role", name, member, role))
            return OrganizationChange(organization=name, member=member)

        async def remove(self, name: str, member: str) -> OrganizationChange:
            actions.append(("remove", name, member))
            return OrganizationChange(organization=name, member=member)

    monkeypatch.setattr(app_module, "OrganizationManager", FakeManager)
    service = service_as(monkeypatch, who)

    created = call(
        service, "POST", "/api/organizations", json={"name": "Lab", "description": "Shared"}
    )
    added = call(
        service,
        "POST",
        "/api/organizations/Lab/members",
        json={"email": "mate@lab.test", "role": "editor"},
    )
    changed = call(
        service, "PUT", "/api/organizations/Lab/members/member-1", json={"role": "admin"}
    )
    removed = call(service, "DELETE", "/api/organizations/Lab/members/member-1")

    assert created.json() == {"organization": "Lab", "member": None}
    assert added.json() == {"organization": "Lab", "member": "mate@lab.test"}
    assert changed.json() == {"organization": "Lab", "member": "member-1"}
    assert removed.json() == {"organization": "Lab", "member": "member-1"}
    assert built == [(who.user, "user-1")] * 4
    assert actions == [
        ("create", "Lab", "Shared"),
        ("add", "Lab", "mate@lab.test", "editor"),
        ("set_role", "Lab", "member-1", "admin"),
        ("remove", "Lab", "member-1"),
    ]


def test_organization_administration_requires_a_matching_current_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = call(
        service_as(monkeypatch, verified()),
        "POST",
        "/api/organizations",
        json={"name": "Lab"},
    )

    assert response.status_code == 403
    assert "current user" in response.json()["detail"]
