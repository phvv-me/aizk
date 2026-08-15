from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import cast

from mangum import Mangum
from mangum.types import LambdaContext
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

_MCP_PATHS = frozenset(
    {
        "/.well-known/oauth-protected-resource/mcp",
    }
)


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
            response = FileResponse(candidate, media_type="text/plain")
            await response(scope, receive, send)
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


def mcp_handler(event: Mapping[str, Json], context: LambdaContext) -> dict[str, Json]:
    """Adapt one Lambda Function URL event to the cached MCP ASGI application."""
    if event.get("kind") == "warm":
        mcp_application()
        return {"warmed": True}
    return mcp_application()(dict(event), context)
