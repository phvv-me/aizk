import asyncio
from dataclasses import dataclass

import pytest
from bg_doubles import fake_runtime
from doubles import AsyncContext

import aizk.commands.aws as aws_mod
from aizk.config import Settings
from aizk.ops import SetupReport
from aizk.runtime import Runtime
from aizk.store.engine import Database


@dataclass
class Context:
    """Minimal Lambda context for handler tests."""

    aws_request_id: str = "request-1"


def test_lambda_drain_assembles_runtime_and_runs_one_worker_wave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = fake_runtime()
    observed: list[Database] = []

    def assemble(cls: type[Runtime], settings: Settings) -> AsyncContext[Runtime]:
        del cls, settings
        return AsyncContext(runtime)

    async def drain_once(received: Runtime) -> int:
        assert received is runtime
        return 3

    monkeypatch.setattr(Runtime, "assemble", classmethod(assemble))
    monkeypatch.setattr(aws_mod, "run_worker_once", drain_once)
    monkeypatch.setattr(aws_mod, "observe", lambda database: observed.append(database))

    assert asyncio.run(aws_mod.drain()) == 3
    assert observed == [runtime.database]


def test_lambda_handlers_return_json_safe_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    async def drained() -> int:
        return 5

    async def setup() -> SetupReport:
        return SetupReport(migrated_from="0003", migrated_to="0004", queue_installed=False)

    monkeypatch.setattr(aws_mod, "drain", drained)
    monkeypatch.setattr(aws_mod.ops, "setup", setup)

    assert aws_mod.worker_handler({}, Context()) == {"handled": 5}
    assert aws_mod.setup_handler({}, Context()) == {
        "migrated_from": "0003",
        "migrated_to": "0004",
        "queue_installed": False,
    }
    assert callable(aws_mod.mcp_handler)
