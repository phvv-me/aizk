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
from starlette.responses import JSONResponse, Response

from ..artifacts.models import ArtifactReceipt
from ..artifacts.service import ArtifactIntake
from ..artifacts.uploads import (
    UploadBox,
    UploadCapabilityError,
    UploadGrant,
    UploadGrantLimitError,
    UploadRequest,
    gather,
)
from ..auth import Auth, Caller
from ..config import settings
from ..exceptions import ScopeNotFoundError
from ..integrations.clamav import MalwareRejectedError, MalwareUnavailableError
from ..integrations.logto import OrganizationChange, OrganizationManager
from ..memory import Memory, WriteResult
from ..storage import ByteLimitExceeded
from ..store.identity import User
from ..types import ScopeNames
from ..usage import annotate_caller
from .artifacts import ArtifactDashboard, ArtifactView
from .dashboard import Dashboard, KnowledgeTotals, RecentSource, UsageTotals
from .middleware import UsageMiddleware
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


class RememberRequest(FrozenModel):
    """One browser memory write, text now or a preserved source URI."""

    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None
    source_uri: str | None = None
    scopes: ScopeNames | None = None
    preserve_source: bool = False

    @model_validator(mode="after")
    def within_deployment_bounds(self) -> Self:
        """Enforce the live deployment limits at validation time instead of import time."""
        if self.text is not None and len(self.text) > settings.mcp_remember_max_chars:
            raise ValueError(f"text must be at most {settings.mcp_remember_max_chars} characters")
        source = self.source_uri or ""
        if len(source) > settings.mcp_source_uri_max_chars:
            raise ValueError(
                f"source_uri must be at most {settings.mcp_source_uri_max_chars} characters"
            )
        if self.scopes is not None and len(self.scopes) > settings.mcp_scope_names_max:
            raise ValueError(f"scopes must name at most {settings.mcp_scope_names_max} entries")
        return self


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


class AizkAPI:
    """Serve the browser JSON surface with the same Logto verification as MCP.

    Every route authenticates a raw bearer token through the shared `Auth`
    verifier as the `verified` dependency, resolves the caller's current Logto
    authority, and leaves PostgreSQL row security as the final boundary. Upload
    capabilities are minted here and by the MCP `remember` upload mode into one
    PostgreSQL-backed store, while only this service redeems them. The FastAPI
    response models make `FastAPI.openapi` the generated web client's contract.
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
                    UploadGrantLimitError,
                    MalwareRejectedError,
                    MalwareUnavailableError,
                    ObjectStoreError,
                    httpx.HTTPError,
                )
            },
            middleware=[Middleware(UsageMiddleware)],
        )
        api.state.service = self
        api.add_api_route("/api/me", self.me, operation_id="me")
        api.add_api_route("/api/overview", self.overview, operation_id="overview")
        api.add_api_route(
            "/api/recall",
            self.recall,
            methods=["POST"],
            operation_id="recall",
            openapi_extra=json_body(RecallRequest),
        )
        api.add_api_route(
            "/api/remember",
            self.remember,
            methods=["POST"],
            operation_id="remember",
            openapi_extra=json_body(RememberRequest),
        )
        api.add_api_route(
            "/api/uploads",
            self.request_upload,
            methods=["POST"],
            operation_id="request_upload",
            openapi_extra=json_body(UploadRequest),
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
            case UploadGrantLimitError():
                return HTTPStatus.TOO_MANY_REQUESTS
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

    async def overview(self, who: Verified) -> Overview:
        """Return knowledge totals, usage, recent sources, and artifact processing states."""
        dashboard = await Dashboard.load(who.user)
        artifacts = await ArtifactDashboard.load(who.user)
        return Overview(**dashboard.model_dump(), **artifacts.model_dump())

    async def recall(self, request: Request, who: Verified) -> Answer:
        """Answer one recall question with merit-ordered Markdown evidence."""
        ask = RecallRequest.model_validate_json(await self.payload(request))
        result = await self.memory(who).recall(ask.query, ask.effective_budget)
        return Answer(markdown=await result.to_markdown())

    async def remember(self, request: Request, who: Verified) -> WriteResult | ArtifactReceipt:
        """Store text now or preserve one URI original, returning its write receipt."""
        ask = RememberRequest.model_validate_json(await self.payload(request))
        return await self.memory(who).remember(
            ask.text,
            source_uri=ask.source_uri,
            scopes=ask.scopes,
            preserve_source=ask.preserve_source,
        )

    async def request_upload(
        self, request: Request, response: Response, who: Verified
    ) -> UploadGrant:
        """Mint one single-use short-TTL capability PUT URL for a declared file."""
        response.headers["Cache-Control"] = "no-store"
        declared = UploadRequest.model_validate_json(await self.payload(request))
        return await self.uploads.mint(who.user, declared)

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
