import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, cast

import boto3
from mangum import Mangum
from mangum.types import LambdaContext
from pydantic import TypeAdapter
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from ..api.app import AizkAPI
from ..artifacts.service import ArtifactIntake
from ..background.wake import configured_worker_wake
from ..config import settings
from ..mcp.runtime import McpRuntime
from ..mcp.server import AizkMCP
from ..store.engine import Database
from ..store.mixins.base import Json
from .aws_observability import instrument

if TYPE_CHECKING:
    from mypy_boto3_lambda import LambdaClient

_MCP_PATHS = frozenset(
    {
        "/.well-known/oauth-protected-resource/mcp",
    }
)
_WEB_PATHS = ("/app", "/auth")
_WEB_RESPONSE = TypeAdapter(dict[str, Json])


class LambdaWebApplication:
    """Forward browser application events to the production SvelteKit Lambda."""

    def __init__(self, function_name: str, client: LambdaClient | None = None) -> None:
        self.function_name = function_name
        self.client = client or boto3.client("lambda")

    def handles(self, event: Mapping[str, Json]) -> bool:
        """Report whether one Function URL request belongs to the web application."""
        path = event.get("rawPath")
        return isinstance(path, str) and any(
            path == prefix or path.startswith(f"{prefix}/") for prefix in _WEB_PATHS
        )

    def forward(self, event: Mapping[str, Json]) -> dict[str, Json]:
        """Invoke the web Lambda synchronously and preserve its HTTP response envelope."""
        response = self.client.invoke(
            FunctionName=self.function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(event, separators=(",", ":")).encode(),
        )
        if error := response.get("FunctionError"):
            raise RuntimeError(f"web application Lambda failed with {error}")
        return _WEB_RESPONSE.validate_json(response["Payload"].read())


class AwsSurface:
    """Route one Lambda origin across MCP metadata, API, configuration, docs, and UI."""

    def __init__(self, mcp: ASGIApp, api: ASGIApp, static_root: Path) -> None:
        self.mcp = mcp
        self.api = api
        self.static_root = static_root.resolve()
        self.static = StaticFiles(directory=static_root, html=True)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Delegate each request without rewriting the path expected by its application."""
        if scope["type"] == "lifespan":
            await self.mcp(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/app-config.json":
            response = JSONResponse(
                {
                    "logtoEndpoint": str(settings.logto_url).rstrip("/"),
                    "appId": settings.spa_client_id,
                    "resource": settings.mcp_resource_id,
                    "callbackPath": "/app/callback",
                },
                headers={"cache-control": "no-store"},
            )
            await response(scope, receive, send)
            return
        if path == "/healthz" or path.startswith("/api/"):
            await self.api(scope, receive, send)
            return
        if path in _MCP_PATHS or path == "/mcp" or path.startswith("/mcp/"):
            await self.mcp(scope, receive, send)
            return
        candidate = (self.static_root / path.lstrip("/")).resolve()
        if (
            candidate.is_relative_to(self.static_root)
            and candidate.suffix == ".md"
            and candidate.is_file()
        ):
            await FileResponse(candidate, media_type="text/plain")(scope, receive, send)
            return
        if candidate.is_relative_to(self.static_root) and candidate.is_dir():
            static_scope = dict(scope)
            static_scope["path"] = f"{path.rstrip('/')}/index.html"
            await self.static(cast("Scope", static_scope), receive, send)
            return
        await self.static(scope, receive, send)


@cache
def mcp_application() -> Mangum:
    """Build the long-lived single-origin application in the public Lambda process."""
    runtime = McpRuntime.assemble(settings)
    instrument(Database.app())
    server = AizkMCP(
        runtime.auth,
        runtime.artifact_store,
        runtime.uploads,
        runtime.artifacts,
        runtime.settings,
        wake=configured_worker_wake(runtime.settings),
    )
    api = AizkAPI(
        runtime.auth,
        runtime.uploads,
        cast("ArtifactIntake", runtime.artifacts),
    ).app()
    application = AwsSurface(
        server.http_app(path="/mcp", stateless_http=True, json_response=True),
        api,
        Path(settings.static_root),
    )
    return Mangum(application, lifespan="auto")


@cache
def web_application() -> LambdaWebApplication | None:
    """Build the optional internal proxy to the production browser application."""
    if not settings.web_function_name:
        return None
    return LambdaWebApplication(settings.web_function_name)


def mcp_handler(event: Mapping[str, Json], context: LambdaContext) -> dict[str, Json]:
    """Adapt one Lambda Function URL event to the cached MCP ASGI application."""
    if event.get("kind") == "warm":
        mcp_application()
        return {"warmed": True}
    web = web_application()
    if web is not None and web.handles(event):
        return web.forward(event)
    return mcp_application()(dict(event), context)
