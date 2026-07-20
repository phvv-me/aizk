from http import HTTPStatus
from typing import Annotated, Self, cast

import httpx
from fastapi import Depends, FastAPI
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from obstore.exceptions import BaseError as ObjectStoreError
from patos import FrozenModel
from pydantic import Field, StringConstraints, ValidationError, model_validator
from pydantic.json_schema import JsonSchemaValue
from pydantic.types import JsonValue
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from ..artifacts.models import ArtifactReceipt
from ..artifacts.service import ArtifactIntake
from ..artifacts.uploads import (
    UploadBox,
    UploadCapabilityError,
    gather,
)
from ..auth import Auth, Caller
from ..config import settings
from ..exceptions import ScopeNotFoundError
from ..integrations.clamav import MalwareRejectedError, MalwareUnavailableError
from ..integrations.logto import OrganizationChange, OrganizationManager
from ..memory import Memory
from ..status import StatusReport
from ..storage import ByteLimitExceeded
from ..store.identity import User
from ..usage import annotate_caller
from .artifacts import ArtifactDashboard, ArtifactView
from .dashboard import Dashboard, KnowledgeTotals, RecentSource, UsageTotals
from .explorer import FindingPage, GraphSlice, SourcePage, SubjectPage, ThemePage
from .middleware import UsageMiddleware
from .operations import ProcessingReport, ProcessingUpdates, UsageReport
from .organizations import OrganizationDirectory


class RecallRequest(FrozenModel):
    """One browser recall question with an optional evidence budget."""

    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    budget: Annotated[int, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def within_deployment_bounds(self) -> Self:
        """Enforce the live deployment limits at validation time instead of import time."""
        if len(self.query) > settings.mcp_recall_query_max_chars:
            raise ValueError(
                f"query must be at most {settings.mcp_recall_query_max_chars} characters"
            )
        if self.budget is not None and self.budget > settings.mcp_recall_budget_max_tokens:
            raise ValueError(
                f"budget must be at most {settings.mcp_recall_budget_max_tokens} tokens"
            )
        return self

    @property
    def effective_budget(self) -> int:
        """The declared budget, or the configured default evidence cap."""
        return self.budget or settings.context_token_budget


class OrganizationRequest(FrozenModel):
    """One new private collaboration space."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    description: str | None = None


class MemberRequest(FrozenModel):
    """One exact-email member addition with its organization role."""

    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    role: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RoleRequest(FrozenModel):
    """One replacement organization role for a current member."""

    role: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Answer(FrozenModel):
    """One recall answer rendered as merit-ordered Markdown evidence."""

    markdown: str


class OrganizationProfile(FrozenModel):
    """One organization membership exactly as `GET /api/me` discloses it."""

    name: str
    description: str | None
    roles: tuple[str, ...]
    permissions: tuple[str, ...]
    writable: bool
    public: bool


class Me(FrozenModel):
    """The signed-in caller's label and current organization standing from Logto."""

    label: str | None
    organizations: tuple[OrganizationProfile, ...]

    @classmethod
    def from_user(cls, user: User) -> Self:
        """Present the verified caller without exposing identifiers or scope internals."""
        return cls(
            label=user.label,
            organizations=tuple(
                OrganizationProfile.model_validate(organization, from_attributes=True)
                for organization in user.organizations
            ),
        )


class Overview(FrozenModel):
    """Knowledge totals, usage, recent sources, and artifact processing states."""

    totals: KnowledgeTotals
    usage: UsageTotals
    recent_sources: tuple[RecentSource, ...]
    artifacts: tuple[ArtifactView, ...]


_BEARER = HTTPBearer(auto_error=False, description="A Logto access token for the aizk resource")


async def verified(
    request: Request,
    token: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)],
) -> Caller:
    """Authenticate one request from its bearer token exactly like the MCP layer."""
    service = cast("AizkAPI", request.app.state.service)
    grant = await service.auth.bearer(token.credentials.strip() if token else "")
    if grant is None:
        raise HTTPException(HTTPStatus.UNAUTHORIZED, "a valid Logto bearer token is required")
    annotate_caller(grant.user)
    return grant


Verified = Annotated[Caller, Depends(verified)]


def json_body(model: type[FrozenModel]) -> JsonSchemaValue:
    """Advertise `model` as one route's JSON request body in the OpenAPI contract.

    The service reads and validates bodies itself through `AizkAPI.payload`, keeping
    the byte budget and failure translation, so the schema is declared explicitly.
    Local `$defs` from named type aliases are inlined because they would dangle once
    the schema is embedded into the larger OpenAPI document.
    """
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(node: JsonValue) -> JsonValue:
        match node:
            case {"$ref": str() as ref} if ref.startswith("#/$defs/"):
                return inline(definitions[ref.removeprefix("#/$defs/")])
            case dict():
                return {key: inline(value) for key, value in node.items()}
            case list():
                return [inline(item) for item in node]
        return node

    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": inline(schema)}},
        }
    }


# The capability PUT streams raw bytes, declared as such in the OpenAPI contract.
_RAW_BODY: JsonSchemaValue = {
    "requestBody": {
        "required": True,
        "content": {
            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
        },
    }
}

_EVENT_STREAM: dict[int | str, dict[str, JsonValue]] = {
    HTTPStatus.OK: {
        "description": "Caller-visible processing snapshots",
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    }
}


class AizkAPI:
    """Serve the browser JSON surface with the same Logto verification as MCP.

    Every route authenticates a raw bearer token through the shared `Auth`
    verifier as the `verified` dependency, resolves the caller's current Logto
    authority, and leaves PostgreSQL row security as the final boundary. Upload
    capabilities are minted by the MCP `remember` upload mode into one
    PostgreSQL-backed store, and only this service redeems them through the
    capability PUT. The FastAPI response models make `FastAPI.openapi` the
    generated web client's contract.
    """

    def __init__(self, auth: Auth, uploads: UploadBox, intake: ArtifactIntake) -> None:
        self.auth = auth
        self.uploads = uploads
        self.intake = intake

    def app(self) -> FastAPI:
        """Assemble explicit typed routes with typed failure translation on every surface."""
        api = FastAPI(
            title="aizk",
            openapi_url=None,
            docs_url=None,
            redoc_url=None,
            exception_handlers={
                exception: self.fail
                for exception in (
                    HTTPException,
                    ValueError,
                    PermissionError,
                    UploadCapabilityError,
                    MalwareRejectedError,
                    MalwareUnavailableError,
                    ObjectStoreError,
                    httpx.HTTPError,
                )
            },
            middleware=[Middleware(UsageMiddleware)],
        )
        api.state.service = self
        api.add_api_route("/healthz", self.health, operation_id="health")
        api.add_api_route("/api/me", self.me, operation_id="me")
        api.add_api_route("/api/status", self.status, operation_id="status")
        api.add_api_route("/api/overview", self.overview, operation_id="overview")
        api.add_api_route("/api/usage", self.usage, operation_id="usage")
        api.add_api_route("/api/processing", self.processing, operation_id="processing")
        api.add_api_route(
            "/api/processing/events",
            self.processing_events,
            operation_id="processing_events",
            response_class=StreamingResponse,
            responses=_EVENT_STREAM,
        )
        api.add_api_route("/api/sources", self.sources, operation_id="sources")
        api.add_api_route("/api/findings", self.findings, operation_id="findings")
        api.add_api_route("/api/subjects", self.subjects, operation_id="subjects")
        api.add_api_route("/api/themes", self.themes, operation_id="themes")
        api.add_api_route("/api/graph", self.graph, operation_id="graph")
        api.add_api_route(
            "/api/recall",
            self.recall,
            methods=["POST"],
            operation_id="recall",
            openapi_extra=json_body(RecallRequest),
        )
        api.add_api_route(
            "/api/uploads/{capability}",
            self.receive_upload,
            methods=["PUT"],
            operation_id="receive_upload",
            openapi_extra=_RAW_BODY,
        )
        api.add_api_route("/api/organizations", self.organizations, operation_id="organizations")
        api.add_api_route(
            "/api/organizations",
            self.create_organization,
            methods=["POST"],
            operation_id="create_organization",
            openapi_extra=json_body(OrganizationRequest),
        )
        api.add_api_route(
            "/api/organizations/{name}/members",
            self.add_member,
            methods=["POST"],
            operation_id="add_member",
            openapi_extra=json_body(MemberRequest),
        )
        api.add_api_route(
            "/api/organizations/{name}/members/{member_id}",
            self.set_member_role,
            methods=["PUT"],
            operation_id="set_member_role",
            openapi_extra=json_body(RoleRequest),
        )
        api.add_api_route(
            "/api/organizations/{name}/members/{member_id}",
            self.remove_member,
            methods=["DELETE"],
            operation_id="remove_member",
        )
        return api

    @staticmethod
    async def health() -> Response:
        """Confirm that the API process can accept HTTP requests."""
        return Response(status_code=HTTPStatus.NO_CONTENT)

    def manager(self, who: Caller) -> OrganizationManager:
        """Return the narrow Logto mutation surface acting as this verified caller."""
        return OrganizationManager(client=self.auth.client, user=who.user, subject=who.subject)

    def memory(self, who: Caller) -> Memory:
        """Build the shared memory service bound to one verified caller."""
        return Memory(user=who.user, intake=self.intake)

    async def fail(self, request: Request, error: Exception) -> JSONResponse:
        """Translate one expected domain failure into its JSON status."""
        return JSONResponse(
            {"detail": self.detail_for(error)},
            status_code=self.status_for(error),
            headers={"Cache-Control": "no-store"},
        )

    @staticmethod
    def detail_for(error: Exception) -> str:
        """Keep domain-authored messages and replace external failures with stable text."""
        match error:
            case HTTPException():
                return error.detail
            case MalwareRejectedError():
                return "the source was rejected by the safety scan"
            case MalwareUnavailableError():
                return "safety scanning is temporarily unavailable"
            case ObjectStoreError():
                return "object storage is temporarily unavailable"
            case httpx.HTTPError():
                return "an upstream request could not be completed"
        return str(error)

    @staticmethod
    def status_for(error: Exception) -> int:
        """Map each expected failure family onto one HTTP status."""
        match error:
            case HTTPException():
                return error.status_code
            case UploadCapabilityError():
                return HTTPStatus.GONE
            case ByteLimitExceeded():
                return HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            case ValidationError() | MalwareRejectedError():
                return HTTPStatus.UNPROCESSABLE_ENTITY
            case ScopeNotFoundError() | PermissionError():
                return HTTPStatus.FORBIDDEN
            case MalwareUnavailableError() | ObjectStoreError():
                return HTTPStatus.SERVICE_UNAVAILABLE
            case httpx.HTTPError():
                return HTTPStatus.BAD_GATEWAY
            case ValueError():
                return HTTPStatus.BAD_REQUEST
        raise TypeError(f"unsupported API failure {type(error).__name__}")

    @staticmethod
    async def payload(request: Request) -> bytes:
        """Read one JSON body while refusing declared or streamed sizes past the API bound."""
        budget = 8 * settings.mcp_remember_max_chars
        declared = request.headers.get("content-length")
        if declared is not None and int(declared) > budget:
            raise ByteLimitExceeded("the request body exceeds the API byte budget")
        return await gather(request.stream(), budget)

    async def me(self, who: Verified) -> Me:
        """Return the caller's label and current organization standing from Logto."""
        return Me.from_user(who.user)

    async def status(self, who: Verified, days: int = 30) -> StatusReport:
        """Return caller authority, durable usage, and processing health."""
        if not 1 <= days <= 365:
            raise ValueError("days must be between 1 and 365")
        return await StatusReport.load(who.user, days)

    async def overview(self, who: Verified) -> Overview:
        """Return knowledge totals, usage, recent sources, and artifact processing states."""
        dashboard = await Dashboard.load(who.user)
        artifacts = await ArtifactDashboard.load(who.user)
        return Overview(**dashboard.model_dump(), **artifacts.model_dump())

    async def usage(self, who: Verified, days: int = 30) -> UsageReport:
        """Return durable caller-owned operation history for one bounded date range."""
        if not 1 <= days <= 365:
            raise ValueError("days must be between 1 and 365")
        return await UsageReport.load(who.user, days)

    async def processing(self, who: Verified) -> ProcessingReport:
        """Return caller-visible processing backlog, throughput, and queue health."""
        return await ProcessingReport.load(who.user)

    async def processing_events(self, request: Request, who: Verified) -> StreamingResponse:
        """Stream caller-visible processing snapshots without buffering or token exposure."""
        updates = ProcessingUpdates(who.user, request.is_disconnected)
        return StreamingResponse(
            updates.events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @staticmethod
    def page(limit: int, offset: int) -> tuple[int, int]:
        """Validate one bounded catalog page request."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        return limit, offset

    async def sources(
        self, who: Verified, search: str = "", limit: int = 50, offset: int = 0
    ) -> SourcePage:
        """Return one read-only page of visible source documents."""
        return await SourcePage.load(who.user, search.strip(), *self.page(limit, offset))

    async def findings(
        self, who: Verified, search: str = "", limit: int = 50, offset: int = 0
    ) -> FindingPage:
        """Return one chronological page of visible current findings."""
        return await FindingPage.load(who.user, search.strip(), *self.page(limit, offset))

    async def subjects(
        self, who: Verified, search: str = "", limit: int = 50, offset: int = 0
    ) -> SubjectPage:
        """Return one page of visible subject claims and graph degrees."""
        return await SubjectPage.load(who.user, search.strip(), *self.page(limit, offset))

    async def themes(self, who: Verified) -> ThemePage:
        """Return every visible graph theme and its bounded member preview."""
        return await ThemePage.load(who.user)

    async def graph(self, who: Verified, limit: int = 40) -> GraphSlice:
        """Return one bounded latest-finding relationship graph."""
        if not 1 <= limit <= 80:
            raise ValueError("limit must be between 1 and 80")
        return await GraphSlice.load(who.user, limit)

    async def recall(self, request: Request, who: Verified) -> Answer:
        """Answer one recall question with merit-ordered Markdown evidence."""
        ask = RecallRequest.model_validate_json(await self.payload(request))
        result = await self.memory(who).recall(ask.query, ask.effective_budget)
        return Answer(markdown=await result.to_markdown())

    async def receive_upload(
        self, capability: str, request: Request, response: Response
    ) -> ArtifactReceipt:
        """Accept one capability's raw bytes through the malware-scanned intake path."""
        response.headers["Cache-Control"] = "no-store"
        ticket = await self.uploads.claim(capability)
        annotate_caller(ticket.user)
        content = await gather(request.stream(), ticket.declared.size)
        return await self.uploads.deliver(ticket, content)

    async def organizations(self, who: Verified) -> OrganizationDirectory:
        """List the caller's authorized memberships from Logto."""
        return await OrganizationDirectory.load(self.auth.client, who.subject)

    async def create_organization(self, request: Request, who: Verified) -> OrganizationChange:
        """Create one private organization owned by the verified caller."""
        ask = OrganizationRequest.model_validate_json(await self.payload(request))
        return await self.manager(who).create(ask.name, ask.description)

    async def add_member(self, name: str, request: Request, who: Verified) -> OrganizationChange:
        """Add one exact-email account to an organization the caller may manage."""
        ask = MemberRequest.model_validate_json(await self.payload(request))
        return await self.manager(who).add(name, ask.email, ask.role)

    async def set_member_role(
        self, name: str, member_id: str, request: Request, who: Verified
    ) -> OrganizationChange:
        """Replace one member's role after a live permission check."""
        ask = RoleRequest.model_validate_json(await self.payload(request))
        return await self.manager(who).set_role(name, member_id, ask.role)

    async def remove_member(self, name: str, member_id: str, who: Verified) -> OrganizationChange:
        """Remove one member after a live permission check."""
        return await self.manager(who).remove(name, member_id)
