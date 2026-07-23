import asyncio
from dataclasses import dataclass

import pytest
from bg_doubles import fake_runtime
from doubles import AsyncContext
from mangum.types import LambdaCognitoIdentity, LambdaMobileClientContext

import aizk.commands.aws as aws_mod
from aizk.artifacts import ArtifactIntake
from aizk.artifacts.uploads import UploadBox
from aizk.auth import Auth
from aizk.background.queue import QueueSnapshot
from aizk.background.wake import NoopWorkerWake, WorkerWake
from aizk.config import Settings
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
    monkeypatch.setattr(aws_mod, "run_worker_once", drain_once)
    monkeypatch.setattr(aws_mod, "Queue", Queue)
    monkeypatch.setattr(aws_mod, "observe", lambda database: observed.append(database))
    aws_mod.instrument.cache_clear()

    expected = {
        "handled": 3,
        "pending": 2,
        "running": 0,
        "failed": 1,
        "last_success_at": None,
        "oldest_queued_at": None,
    }
    assert asyncio.run(aws_mod.drain()) == expected
    assert asyncio.run(aws_mod.drain()) == expected
    assert observed == [runtime.database]


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

    monkeypatch.setattr(aws_mod, "drain", drained)
    monkeypatch.setattr(aws_mod.ops, "setup", setup)

    assert aws_mod.worker_handler({}, Context()) == worker_report
    assert aws_mod.setup_handler({}, Context()) == {
        "migrated_from": "0003",
        "migrated_to": "0004",
        "queue_installed": False,
    }
    assert callable(aws_mod.mcp_handler)


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

    monkeypatch.setattr(aws_mod, "drain", drained)

    with pytest.raises(RuntimeError, match="2 terminal failures"):
        aws_mod.worker_handler({}, Context())


def test_mcp_handler_builds_and_reuses_one_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = fake_runtime()
    expected_wake = NoopWorkerWake()
    calls: list[tuple[dict[str, Json], Context]] = []
    builds: list[int] = []
    observed: list[Database] = []

    class Application:
        def __call__(self, event: dict[str, Json], context: Context) -> dict[str, Json]:
            calls.append((event, context))
            return {"body": "ok"}

    class Server:
        def http_app(self, *, path: str, stateless_http: bool) -> str:
            assert (path, stateless_http) == ("/mcp", True)
            return "asgi"

    def assemble(cls: type[Runtime], config: Settings) -> Runtime:
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
            runtime.store,
            runtime.uploads,
            runtime.artifacts.intake,
            runtime.settings,
            "aizk",
            expected_wake,
        )
        return Server()

    def application(asgi: str, *, lifespan: str) -> Application:
        assert (asgi, lifespan) == ("asgi", "auto")
        builds.append(1)
        return Application()

    monkeypatch.setattr(Runtime, "assemble", classmethod(assemble))
    monkeypatch.setattr(aws_mod, "AizkMCP", server)
    monkeypatch.setattr(aws_mod, "Mangum", application)
    monkeypatch.setattr(aws_mod, "configured_worker_wake", lambda config: expected_wake)
    monkeypatch.setattr(aws_mod, "instrument", lambda database: observed.append(database))
    aws_mod.mcp_application.cache_clear()
    event: dict[str, Json] = {"version": "2.0"}
    context = Context()

    assert aws_mod.mcp_handler(event, context) == {"body": "ok"}
    assert aws_mod.mcp_handler(event, context) == {"body": "ok"}
    assert builds == [1]
    assert observed == [runtime.database]
    assert calls == [(event, context), (event, context)]
