import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path

import mcp.types as mt
import pytest
from bg_doubles import fake_runtime
from doubles import AsyncContext
from mangum.types import LambdaCognitoIdentity, LambdaMobileClientContext
from mcp_probe import USER_TOOLS
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp

import aizk.commands.aws_mcp as mcp_mod
import aizk.commands.aws_observability as observability_mod
import aizk.commands.aws_worker as worker_mod
from aizk.artifacts.service import ArtifactIntake
from aizk.artifacts.uploads import UploadBox
from aizk.auth import Auth
from aizk.background.queue import QueueSnapshot
from aizk.background.wake import NoopWorkerWake, WorkerWake
from aizk.config import Settings
from aizk.mcp.runtime import McpRuntime, TextOnlyArtifacts
from aizk.ops import SetupReport
from aizk.runtime import Runtime
from aizk.storage import ByteStore
from aizk.store.engine import Database
from aizk.store.mixins.base import Json


@dataclass
class Context:
    """Minimal Lambda context for handler tests."""

    function_name: str = "aizk-test"
    function_version: str = "$LATEST"
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:aizk-test"
    memory_limit_in_mb: int = 1024
    aws_request_id: str = "request-1"
    log_group_name: str = "/aws/lambda/aizk-test"
    log_stream_name: str = "test"
    identity: LambdaCognitoIdentity | None = None
    client_context: LambdaMobileClientContext | None = None

    def get_remaining_time_in_millis(self) -> int:
        return 1000


def test_lambda_drain_assembles_runtime_and_runs_one_worker_wave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = fake_runtime()
    observed: list[Database] = []

    class Queue:
        def __init__(self, dsn: str) -> None:
            assert dsn

        async def __aenter__(self) -> Queue:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def snapshot(self) -> QueueSnapshot:
            return QueueSnapshot(
                pending=2,
                running=0,
                failed=1,
                last_success=None,
                oldest_queued=None,
            )

    def assemble(cls: type[Runtime], settings: Settings) -> AsyncContext[Runtime]:
        del cls, settings
        return AsyncContext(runtime)

    async def drain_once(received: Runtime) -> int:
        assert received is runtime
        return 3

    monkeypatch.setattr(Runtime, "assemble", classmethod(assemble))
    monkeypatch.setattr(worker_mod, "run_worker_once", drain_once)
    monkeypatch.setattr(worker_mod, "Queue", Queue)
    monkeypatch.setattr(worker_mod, "instrument", lambda database: observed.append(database))

    expected = {
        "handled": 3,
        "pending": 2,
        "running": 0,
        "failed": 1,
        "last_success_at": None,
        "oldest_queued_at": None,
    }
    assert asyncio.run(worker_mod.drain()) == expected
    assert asyncio.run(worker_mod.drain()) == expected
    assert observed == [runtime.database, runtime.database]


def test_lambda_handlers_return_json_safe_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    worker_report: dict[str, Json] = {
        "handled": 5,
        "pending": 1,
        "running": 0,
        "failed": 0,
        "last_success_at": "2026-07-23T00:00:00+00:00",
        "oldest_queued_at": None,
    }

    async def drained() -> dict[str, Json]:
        return worker_report

    async def setup() -> SetupReport:
        return SetupReport(migrated_from="0003", migrated_to="0004", queue_installed=False)

    monkeypatch.setattr(worker_mod, "drain", drained)
    monkeypatch.setattr(worker_mod.ops, "setup", setup)

    assert worker_mod.worker_handler({"kind": "worker"}, Context()) == worker_report
    assert worker_mod.worker_handler({"kind": "setup"}, Context()) == {
        "migrated_from": "0003",
        "migrated_to": "0004",
        "queue_installed": False,
    }
    assert callable(mcp_mod.mcp_handler)


def test_lambda_worker_fails_the_invocation_for_retained_queue_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def drained() -> dict[str, Json]:
        return {
            "handled": 1,
            "pending": 0,
            "running": 0,
            "failed": 2,
            "last_success_at": None,
            "oldest_queued_at": None,
        }

    monkeypatch.setattr(worker_mod, "drain", drained)

    with pytest.raises(RuntimeError, match="2 terminal failures"):
        worker_mod.worker_handler({"kind": "worker"}, Context())


@pytest.mark.parametrize("event", [{}, {"kind": "unknown"}])
def test_lambda_worker_rejects_implicit_or_unknown_work(event: dict[str, Json]) -> None:
    with pytest.raises(ValueError, match="unsupported worker event kind"):
        worker_mod.worker_handler(event, Context())


def test_mcp_handler_builds_and_reuses_one_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete = fake_runtime()
    artifacts = TextOnlyArtifacts()
    runtime = McpRuntime(
        settings=complete.settings,
        artifacts=artifacts,
        uploads=complete.uploads,
        auth=complete.auth,
    )
    expected_wake = NoopWorkerWake()
    calls: list[tuple[dict[str, Json], Context]] = []
    builds: list[int] = []
    observed: list[Database] = []

    class Application:
        def __call__(self, event: dict[str, Json], context: Context) -> dict[str, Json]:
            calls.append((event, context))
            return {"body": "ok"}

    class Server:
        def http_app(
            self,
            *,
            path: str,
            stateless_http: bool,
            json_response: bool,
        ) -> str:
            assert (path, stateless_http, json_response) == ("/mcp", True, True)
            return "asgi"

    def assemble(cls: type[McpRuntime], config: Settings) -> McpRuntime:
        del cls
        assert config is runtime.settings
        return runtime

    def server(
        auth: Auth,
        store: ByteStore,
        uploads: UploadBox,
        intake: ArtifactIntake,
        config: Settings,
        name: str = "aizk",
        wake: WorkerWake | None = None,
    ) -> Server:
        assert (auth, store, uploads, intake, config, name, wake) == (
            runtime.auth,
            artifacts,
            runtime.uploads,
            artifacts,
            runtime.settings,
            "aizk",
            expected_wake,
        )
        return Server()

    def application(asgi: str, *, lifespan: str) -> Application:
        assert (asgi, lifespan) == ("asgi", "auto")
        builds.append(1)
        return Application()

    def surface(mcp: str, api: ASGIApp, static_root: Path) -> str:
        assert mcp == "asgi"
        assert api is not None
        assert static_root == runtime.settings.static_root
        return "asgi"

    monkeypatch.setattr(McpRuntime, "assemble", classmethod(assemble))
    monkeypatch.setattr(mcp_mod.Database, "app", lambda: complete.database)
    monkeypatch.setattr(mcp_mod, "AizkMCP", server)
    monkeypatch.setattr(mcp_mod, "Mangum", application)
    monkeypatch.setattr(mcp_mod, "AwsSurface", surface)
    monkeypatch.setattr(mcp_mod, "configured_worker_wake", lambda config: expected_wake)
    monkeypatch.setattr(mcp_mod, "instrument", lambda database: observed.append(database))
    mcp_mod.mcp_application.cache_clear()
    event: dict[str, Json] = {"version": "2.0"}
    context = Context()

    assert mcp_mod.mcp_handler(event, context) == {"body": "ok"}
    assert mcp_mod.mcp_handler(event, context) == {"body": "ok"}
    assert builds == [1]
    assert observed == [complete.database]
    assert calls == [(event, context), (event, context)]


@pytest.mark.parametrize(
    "path",
    [
        "/mcp",
        "/mcp/resource",
        "/.well-known/oauth-protected-resource/mcp",
    ],
)
def test_aws_surface_routes_mcp_and_its_metadata_to_fastmcp(tmp_path: Path, path: str) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("static")
    mcp = Starlette(routes=[Route("/{path:path}", lambda request: PlainTextResponse("mcp"))])
    api = Starlette(routes=[Route("/{path:path}", lambda request: PlainTextResponse("api"))])

    with TestClient(mcp_mod.AwsSurface(mcp, api, static)) as client:
        response = client.get(path)

    assert response.text == "mcp"


def test_aws_surface_routes_configuration_api_and_static_directories(tmp_path: Path) -> None:
    static = tmp_path / "static"
    nested = static / "guide"
    nested.mkdir(parents=True)
    (static / "index.html").write_text("home")
    (static / "asset.txt").write_text("asset")
    (nested / "index.html").write_text("guide")
    mcp = Starlette(routes=[Route("/{path:path}", lambda request: PlainTextResponse("mcp"))])
    api = Starlette(routes=[Route("/{path:path}", lambda request: PlainTextResponse("api"))])

    with TestClient(mcp_mod.AwsSurface(mcp, api, static)) as client:
        configuration = client.get("/app-config.json")
        health = client.get("/healthz")
        endpoint = client.get("/api/status")
        guide = client.get("/guide")
        home = client.get("/")
        asset = client.get("/asset.txt")

    assert set(configuration.json()) == {
        "logtoEndpoint",
        "appId",
        "resource",
        "callbackPath",
    }
    assert configuration.headers["cache-control"] == "no-store"
    assert health.text == "api"
    assert endpoint.text == "api"
    assert guide.text == "guide"
    assert home.text == "home"
    assert asset.text == "asset"


def test_lambda_mcp_warm_event_builds_the_cached_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds: list[int] = []
    monkeypatch.setattr(mcp_mod, "mcp_application", lambda: builds.append(1))

    assert mcp_mod.mcp_handler({"kind": "warm"}, Context()) == {"warmed": True}
    assert builds == [1]


def test_lambda_observability_is_installed_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = fake_runtime().database
    observed: list[Database] = []
    monkeypatch.setattr(observability_mod, "observe", lambda target: observed.append(target))
    observability_mod.instrument.cache_clear()

    observability_mod.instrument(database)
    observability_mod.instrument(database)

    assert observed == [database]
    observability_mod.instrument.cache_clear()


def test_lambda_mcp_accepts_only_the_modern_protocol(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    complete = replace(fake_runtime(), auth=Auth())
    runtime = McpRuntime(
        settings=complete.settings,
        artifacts=TextOnlyArtifacts(),
        uploads=complete.uploads,
        auth=complete.auth,
    )

    def event(
        method: str,
        request_id: int,
        params: dict[str, Json],
        protocol_version: str,
    ) -> dict[str, Json]:
        return {
            "version": "2.0",
            "routeKey": "$default",
            "rawPath": "/mcp",
            "rawQueryString": "",
            "headers": {
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "host": "lambda.test",
                "mcp-method": method,
                "mcp-protocol-version": protocol_version,
            },
            "requestContext": {
                "accountId": "123",
                "apiId": "test",
                "domainName": "lambda.test",
                "domainPrefix": "test",
                "http": {
                    "method": "POST",
                    "path": "/mcp",
                    "protocol": "HTTP/1.1",
                    "sourceIp": "127.0.0.1",
                    "userAgent": "lambda-probe",
                },
                "requestId": f"request-{request_id}",
                "routeKey": "$default",
                "stage": "$default",
                "time": "10/Aug/2026:00:00:00 +0000",
                "timeEpoch": 0,
            },
            "body": json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            ),
            "isBase64Encoded": False,
        }

    def invoke(payload: dict[str, Json]) -> dict[str, Json]:
        return mcp_mod.mcp_handler(payload, Context())

    monkeypatch.setattr(
        McpRuntime,
        "assemble",
        classmethod(lambda cls, config: runtime),
    )
    monkeypatch.setattr(mcp_mod.settings, "static_root", tmp_path)
    monkeypatch.setattr(mcp_mod.Database, "app", lambda: complete.database)
    monkeypatch.setattr(mcp_mod, "instrument", lambda database: None)
    mcp_mod.mcp_application.cache_clear()

    modern_meta: dict[str, Json] = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {
            "name": "modern-lambda",
            "version": "1",
        },
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    discovered = invoke(
        event(
            "server/discover",
            1,
            {"_meta": modern_meta},
            "2026-07-28",
        )
    )
    assert discovered["statusCode"] == 200
    discovered_headers = discovered["headers"]
    discovered_body = discovered["body"]
    assert isinstance(discovered_headers, dict)
    assert isinstance(discovered_body, str)
    assert discovered_headers["content-type"] == "application/json"
    modern_result = json.loads(discovered_body)["result"]
    assert modern_result["supportedVersions"] == ["2026-07-28"]

    listed = invoke(
        event(
            "tools/list",
            2,
            {"_meta": modern_meta},
            "2026-07-28",
        )
    )
    assert listed["statusCode"] == 200
    listed_body = listed["body"]
    assert isinstance(listed_body, str)
    listed_result = json.loads(listed_body)["result"]
    assert {tool["name"] for tool in listed_result["tools"]} == USER_TOOLS

    rejected = invoke(
        event(
            "initialize",
            3,
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "legacy-lambda", "version": "1"},
            },
            "2025-11-25",
        )
    )
    assert rejected["statusCode"] == 200
    rejected_body = rejected["body"]
    assert isinstance(rejected_body, str)
    error = json.loads(rejected_body)["error"]
    assert error == {
        "code": mt.UNSUPPORTED_PROTOCOL_VERSION,
        "message": "AIZK requires MCP 2026-07-28",
        "data": {"supported": ["2026-07-28"], "requested": "2025-11-25"},
    }
    mcp_mod.mcp_application.cache_clear()
