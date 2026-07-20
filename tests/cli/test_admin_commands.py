import json
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from id_factory import uuid7
from pydantic import AnyHttpUrl, SecretStr

import aizk.commands.admin as commands
from aizk.integrations.logto import PolicyReport

_DOCUMENT_ID = uuid.UUID("01900000-0000-7000-8000-000000000001")


class Jsonable:
    def __init__(self, payload: str = '{"ok": true}') -> None:
        self.payload = payload

    def model_dump_json(self, indent: int | None = None) -> str:
        return self.payload


class Rendered:
    def render(self) -> str:
        return "EXPORT"


class FakeRuntime:
    def __init__(self) -> None:
        self.auth = Mock()
        self.store = Mock()
        self.uploads = Mock()
        self.database = Mock()
        self.artifacts = SimpleNamespace(intake=Mock())
        self.settings = commands.settings
        self.graph = Mock()
        self.llm = Mock()
        self.embed = Mock()
        self.extractor = Mock()
        self.closes = 0

    async def __aenter__(self) -> FakeRuntime:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self.closes += 1


class Seams:
    def __init__(self) -> None:
        self.runtime = FakeRuntime()
        self.profile = MagicMock()
        self.profile.report.return_value = "profile"
        self.profiler = Mock(return_value=self.profile)
        self.profiler.Feature = commands.Profiler.Feature
        self.observe = Mock()
        self.setup = AsyncMock(return_value=SimpleNamespace(migrated_to="head"))
        self.worker = AsyncMock()
        self.serve_mcp = AsyncMock()


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> Seams:
    seams = Seams()
    monkeypatch.setattr(commands, "Profiler", seams.profiler)
    monkeypatch.setattr(commands, "observe", seams.observe)
    monkeypatch.setattr(commands.ops, "setup", seams.setup)
    monkeypatch.setattr(commands, "run_worker", seams.worker)
    monkeypatch.setattr(
        commands,
        "AizkMCP",
        Mock(return_value=SimpleNamespace(run_http_async=seams.serve_mcp)),
    )
    monkeypatch.setattr(
        commands.Runtime,
        "assemble",
        classmethod(lambda cls, config: seams.runtime),
    )
    return seams


def dispatch(tokens: list[str]) -> None:
    commands.admin_app(tokens, exit_on_error=False, result_action="return_value")


def command_names(app: commands.App) -> set[str]:
    return set(app.resolved_commands()) - set(app.help_flags) - set(app.version_flags)


def test_operator_tree_has_one_explicit_admin_boundary() -> None:
    assert command_names(commands.admin_app) == {
        "api",
        "auth",
        "data",
        "database",
        "graph",
        "health",
        "ontology",
        "queue",
        "server",
        "settings",
    }
    assert command_names(commands.server_app) == {"api", "mcp", "worker"}
    assert command_names(commands.queue_app) == {"doctor", "retry", "status"}
    assert command_names(commands.retry_app) == {"conversion", "graph", "profile"}
    assert command_names(commands.database_app) == {
        "backup",
        "check-rls",
        "install-queue",
        "make-migration",
        "migrate",
        "reset",
        "restore",
        "setup",
    }
    assert command_names(commands.graph_app) == {
        "communities",
        "decay",
        "diagnose-extraction",
        "forget",
        "raptor",
        "rebuild",
        "reembed",
    }
    assert command_names(commands.data_app) == {"audit", "export", "ingest", "promote"}
    assert command_names(commands.ontology_app) == {
        "define-entity",
        "define-relation",
        "list",
    }
    assert command_names(commands.auth_app) == {"apply", "audit", "check-public", "check-web"}
    assert command_names(commands.settings_app) == {"show", "validate"}
    assert command_names(commands.api_app) == {"openapi"}


@pytest.mark.parametrize(
    ("tokens", "target", "result", "expected"),
    [
        (
            ["queue", "status"],
            "tasks_status",
            Jsonable(),
            '{"ok": true}',
        ),
        (
            ["database", "setup"],
            "setup",
            SimpleNamespace(migrated_from="a", migrated_to="b"),
            "migrated a -> b",
        ),
        (
            ["graph", "decay", "--half-life-days", "30"],
            "decay",
            4,
            "archived 4",
        ),
        (
            ["graph", "reembed"],
            "reembed",
            9,
            "re-embedded 9",
        ),
        (
            ["graph", "communities"],
            "communities",
            3,
            "built 3 communities",
        ),
        (
            ["graph", "forget", "wrong note"],
            "forget",
            SimpleNamespace(claims=6, documents=["A", "B"]),
            "retracted 6 claims from 2 notes",
        ),
        (
            ["data", "ingest", "notes"],
            "ingest",
            4,
            "ingested 4 documents",
        ),
        (
            ["data", "promote", str(_DOCUMENT_ID), "team"],
            "promote",
            5,
            "promoted 5 document into team",
        ),
        (
            ["data", "export", "dump.jsonl"],
            "export_scope",
            Rendered(),
            "EXPORT",
        ),
        (
            ["ontology", "define-entity", "Area", "A domain"],
            "define_entity_kind",
            None,
            "entity kind Area defined",
        ),
        (
            ["ontology", "define-relation", "funds", "X funds Y"],
            "define_relation_kind",
            None,
            "relation kind funds defined",
        ),
        (
            ["health"],
            "health",
            Jsonable(),
            '{"ok": true}',
        ),
    ],
    ids=[
        "queue-status",
        "database-setup",
        "graph-decay",
        "graph-reembed",
        "graph-communities",
        "graph-forget",
        "data-ingest",
        "data-promote",
        "data-export",
        "ontology-entity",
        "ontology-relation",
        "health",
    ],
)
def test_commands_route_to_operator_services(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tokens: list[str],
    target: str,
    result: int | Jsonable | Rendered | SimpleNamespace | None,
    expected: str,
) -> None:
    recorder = AsyncMock(return_value=result)
    monkeypatch.setattr(commands.admin, target, recorder)

    dispatch(tokens)

    assert recorder.call_count == 1
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize(
    ("tokens", "target", "count", "expected"),
    [
        (["queue", "retry", "conversion", "--limit", "7"], "conversion", 5, "conversion"),
        (["queue", "retry", "graph", "--limit", "9"], "graph", 6, "graph"),
        (["queue", "retry", "profile", "--limit", "11"], "profile", 7, "profile"),
    ],
)
def test_retry_commands_use_the_typed_queue_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tokens: list[str],
    target: str,
    count: int,
    expected: str,
) -> None:
    recorder = AsyncMock(return_value=count)
    if target == "conversion":
        monkeypatch.setattr(commands, "retry_failed_artifacts", recorder)
    elif target == "graph":
        monkeypatch.setattr(commands, "retry_failed_chunks", recorder)
    else:
        monkeypatch.setattr(commands, "retry_failed_profile_projections", recorder)

    dispatch(tokens)

    assert recorder.call_args.args == (int(tokens[-1]),)
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize(
    ("tokens", "recorder_name", "expected_args", "expected_kwargs", "expected"),
    [
        (
            ["database", "migrate"],
            "run_alembic",
            (commands.command.upgrade, "CONFIG", "head"),
            {"sql": False},
            "done",
        ),
        (
            ["database", "migrate", "--sql"],
            "run_alembic",
            (commands.command.upgrade, "CONFIG", "head"),
            {"sql": True},
            "",
        ),
        (
            ["database", "make-migration", "add field"],
            "run_alembic",
            (commands.command.revision, "CONFIG"),
            {"message": "add field", "autogenerate": True},
            "done",
        ),
        (
            ["database", "install-queue"],
            "install_queue",
            (),
            {},
            "done",
        ),
        (
            ["database", "backup", "/tmp/a.dump"],
            "backup",
            ("/tmp/a.dump",),
            {},
            "backed up 7 bytes",
        ),
        (
            ["database", "restore", "/tmp/a.dump"],
            "restore",
            ("/tmp/a.dump",),
            {},
            "restored /tmp/a.dump",
        ),
    ],
)
def test_database_commands_dispatch_exact_arguments(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tokens: list[str],
    recorder_name: str,
    expected_args: tuple[Callable[..., None] | str, ...],
    expected_kwargs: dict[str, str | bool],
    expected: str,
) -> None:
    recorders = {
        "run_alembic": Mock(),
        "install_queue": AsyncMock(),
        "backup": AsyncMock(return_value=SimpleNamespace(bytes=7, path="/tmp/a.dump")),
        "restore": AsyncMock(return_value=SimpleNamespace(path="/tmp/a.dump", database="aizk")),
    }
    monkeypatch.setattr(commands.ops, "alembic_config", Mock(return_value="CONFIG"))
    monkeypatch.setattr(commands.ops, "run_alembic", recorders["run_alembic"])
    monkeypatch.setattr(commands, "install_queue_schema", recorders["install_queue"])
    monkeypatch.setattr(commands.backup_ops, "backup_database", recorders["backup"])
    monkeypatch.setattr(commands.backup_ops, "restore_database", recorders["restore"])

    dispatch(tokens)

    recorder = cast("Mock", recorders[recorder_name])
    assert recorder.call_args.args == expected_args
    assert recorder.call_args.kwargs == expected_kwargs
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize("violations", [[], ["document misses FORCE ROW LEVEL SECURITY"]])
def test_rls_check_exits_only_for_violations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    violations: list[str],
) -> None:
    monkeypatch.setattr(commands.ops, "scoped_rls_violations", AsyncMock(return_value=violations))

    if violations:
        with pytest.raises(SystemExit) as exit_info:
            dispatch(["database", "check-rls"])
        assert exit_info.value.code == 1
    else:
        dispatch(["database", "check-rls"])

    assert (violations[0] if violations else "ok") in capsys.readouterr().out


def test_database_reset_requires_exact_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reset = AsyncMock(
        return_value=SimpleNamespace(database=commands.settings.db_name, migrated_to="head")
    )
    monkeypatch.setattr(commands.admin, "reset_database", reset)

    with pytest.raises(ValueError, match="confirmation"):
        dispatch(["database", "reset", "wrong"])
    dispatch(["database", "reset", commands.settings.db_name])

    assert reset.call_count == 1
    assert "at head" in capsys.readouterr().out


@pytest.mark.parametrize("healthy", [True, False])
def test_doctor_is_stable_json_and_fails_for_live_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    healthy: bool,
) -> None:
    report = SimpleNamespace(
        healthy=healthy,
        model_dump=Mock(return_value={"z": 1, "healthy": healthy, "a": 2}),
    )
    diagnose = AsyncMock(return_value=report)
    monkeypatch.setattr(commands.ops, "doctor", diagnose)
    tokens = [
        "queue",
        "doctor",
        "--stale-minutes",
        "3",
        "--long-running-minutes",
        "7",
        "--history-hours",
        "2",
        "--limit",
        "11",
    ]

    if healthy:
        dispatch(tokens)
    else:
        with pytest.raises(SystemExit) as exit_info:
            dispatch(tokens)
        assert exit_info.value.code == 1

    output = capsys.readouterr().out
    assert output.index('"a"') < output.index('"healthy"') < output.index('"z"')
    assert diagnose.call_args.kwargs == {
        "stale_minutes": 3,
        "long_running_minutes": 7,
        "history_hours": 2,
        "limit": 11,
        "show_error_messages": False,
    }


@pytest.mark.parametrize("profiling", [True, False])
def test_worker_runs_through_optional_profiler(
    seams: Seams,
    monkeypatch: pytest.MonkeyPatch,
    profiling: bool,
) -> None:
    monkeypatch.setattr(commands.settings, "profiling", profiling)

    dispatch(["server", "worker", "--batch-size", "7"])

    assert seams.worker.call_args.kwargs == {"batch_size": 7}
    assert seams.profiler.call_count == int(profiling)
    assert seams.profile.report.call_count == int(profiling)
    assert seams.runtime.closes == 1


@pytest.mark.parametrize("with_worker", [True, False])
@pytest.mark.parametrize("auto_setup", [True, False])
def test_mcp_server_runs_with_configured_colocation(
    seams: Seams,
    monkeypatch: pytest.MonkeyPatch,
    with_worker: bool,
    auto_setup: bool,
) -> None:
    monkeypatch.setattr(commands.settings, "serve_with_worker", with_worker)
    monkeypatch.setattr(commands.settings, "auto_setup", auto_setup)
    monkeypatch.setattr(commands.settings, "profiling", False)

    dispatch(["server", "mcp"])

    assert seams.serve_mcp.await_count == 1
    assert seams.worker.await_count == int(with_worker)
    assert seams.setup.await_count == int(auto_setup)


@pytest.mark.parametrize("auto_setup", [True, False])
def test_api_server_runs_uvicorn(
    seams: Seams,
    monkeypatch: pytest.MonkeyPatch,
    auto_setup: bool,
) -> None:
    serve = AsyncMock()
    config = Mock(return_value="CONFIG")
    server = Mock(return_value=SimpleNamespace(serve=serve))
    monkeypatch.setattr(commands.settings, "auto_setup", auto_setup)
    monkeypatch.setattr(commands, "uvicorn", SimpleNamespace(Config=config, Server=server))

    dispatch(["server", "api"])

    assert serve.await_count == 1
    assert config.call_args.kwargs == {
        "host": commands.settings.api_host,
        "port": commands.settings.api_port,
    }
    assert seams.setup.await_count == int(auto_setup)


def test_runtime_graph_commands_receive_assembled_clients(
    seams: Seams,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rebuild = AsyncMock(return_value=(3, 7))
    diagnose = AsyncMock(return_value=Jsonable('{"grounding": []}'))
    raptor = AsyncMock(return_value=2)
    monkeypatch.setattr(commands.admin, "rebuild", rebuild)
    monkeypatch.setattr(commands.admin, "diagnose_extraction", diagnose)
    monkeypatch.setattr(commands.admin, "raptor", raptor)

    dispatch(["graph", "rebuild", "--limit", "5"])
    dispatch(["graph", "diagnose-extraction", str(_DOCUMENT_ID)])
    dispatch(["graph", "raptor"])

    assert rebuild.call_args.args == (seams.runtime.graph,)
    assert rebuild.call_args.kwargs["limit"] == 5
    assert diagnose.call_args.args == (seams.runtime.extractor, _DOCUMENT_ID)
    assert raptor.call_args.args[:2] == (seams.runtime.llm, seams.runtime.embed)
    assert "3 entities and 7 facts" in capsys.readouterr().out


def test_data_audit_and_ontology_list_render_rows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scope = uuid.UUID("44444444-4444-4444-4444-444444444444")
    documents = [
        SimpleNamespace(
            id=_DOCUMENT_ID,
            subject_type="project",
            scopes=[scope],
            title="Shared",
        ),
        SimpleNamespace(id=uuid7(), subject_type=None, scopes=[], title=None),
    ]
    ontology = [
        SimpleNamespace(
            structural=False,
            kind="entity",
            name="Concept",
            domain="general",
            uses=3,
        ),
        SimpleNamespace(
            structural=True,
            kind="entity",
            name="Theme",
            domain="core",
            uses=1,
        ),
    ]
    monkeypatch.setattr(commands.admin, "audit", AsyncMock(return_value=documents))
    monkeypatch.setattr(commands.admin, "list_ontology", AsyncMock(return_value=ontology))

    dispatch(["data", "audit"])
    dispatch(["ontology", "list"])

    output = capsys.readouterr().out
    assert f"[{scope}]  Shared" in output
    assert "[private]  -" in output
    assert "  entity   Concept" in output
    assert "* entity   Theme" in output


@pytest.mark.parametrize(
    ("command_name", "clean", "exits"),
    [("audit", True, False), ("audit", False, True), ("apply", True, False)],
)
def test_auth_policy_commands_close_the_management_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command_name: str,
    clean: bool,
    exits: bool,
) -> None:
    client = SimpleNamespace(close=AsyncMock())
    report = PolicyReport(clean=clean, changes=() if clean else ("repair",))
    policy = SimpleNamespace(
        audit=AsyncMock(return_value=report),
        apply=AsyncMock(return_value=report),
    )
    monkeypatch.setattr(commands, "LogtoClient", Mock(return_value=client))
    monkeypatch.setattr(commands, "LogtoPolicy", Mock(return_value=policy))

    if exits:
        with pytest.raises(SystemExit) as exit_info:
            dispatch(["auth", command_name])
        assert exit_info.value.code == 1
    else:
        dispatch(["auth", command_name])

    assert getattr(policy, command_name).await_count == 1
    assert client.close.await_count == 1
    assert f'"clean": {str(clean).lower()}' in capsys.readouterr().out


def test_auth_configuration_checks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatch(["auth", "check-public"])
    monkeypatch.setattr(commands.settings, "web_public_url", None)
    with pytest.raises(RuntimeError, match="web_public_url"):
        dispatch(["auth", "check-web"])
    monkeypatch.setattr(
        commands.settings,
        "web_public_url",
        AnyHttpUrl("https://memory.test"),
    )
    dispatch(["auth", "check-web"])

    output = capsys.readouterr().out
    assert "public authentication configuration is complete" in output
    assert "https://memory.test/auth/sign-in-callback" in output


def test_settings_show_is_sorted_and_redacts_every_secret_shape(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(commands.settings, "admin_database_url", "postgresql://admin:pw@db/aizk")
    monkeypatch.setattr(commands.settings, "app_password", "pw")
    monkeypatch.setattr(commands.settings, "embed_api_key", "key")
    monkeypatch.setattr(commands.settings, "oauth_client_secret", SecretStr("secret"))

    dispatch(["settings", "show"])
    payload = json.loads(capsys.readouterr().out)

    assert list(payload) == sorted(payload)
    assert payload["admin_database_url"] == "<redacted>"
    assert payload["app_password"] == "<redacted>"
    assert payload["embed_api_key"] == "<redacted>"
    assert payload["oauth_client_secret"] == "<redacted>"
    assert payload["api_host"] == commands.settings.api_host

    dispatch(["settings", "show", "api-host"])
    assert json.loads(capsys.readouterr().out) == {"api_host": commands.settings.api_host}

    with pytest.raises(ValueError, match="unknown setting"):
        dispatch(["settings", "show", "missing"])


def test_settings_validate_reloads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    validate = Mock()
    monkeypatch.setattr(commands, "Settings", validate)

    dispatch(["settings", "validate"])

    assert validate.call_count == 1
    assert json.loads(capsys.readouterr().out) == {"valid": True}


def test_openapi_writes_the_browser_schema(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "openapi.json"

    dispatch(["api", "openapi", str(target)])

    schema = json.loads(target.read_text(encoding="utf-8"))
    assert "/api/processing" in schema["paths"]
    assert f"wrote {target}" in capsys.readouterr().out
