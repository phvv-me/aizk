import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from id_factory import uuid5, uuid7
from pydantic import UUID5, UUID7

import aizk.cli as cli
from aizk.common.auth.logto import PolicyReport
from aizk.config import Settings
from aizk.retrieval import Candidate, Lane
from aizk.store.identity import User

_DOC_ID = uuid7()
_USER_ID = uuid7()
_OTHER_USER_ID = uuid5()


class Rendered:
    def __init__(self, text: str) -> None:
        self.text = text

    def render(self) -> str:
        return self.text


class Seams:
    def __init__(self) -> None:
        self.run_alembic = Mock()
        self.alembic_config = Mock(return_value="CONFIG")
        self.setup = AsyncMock(return_value=SimpleNamespace(migrated_to="head"))
        self.rls = AsyncMock(return_value=[])
        self.enable_spans = Mock()
        self.worker = AsyncMock()
        self.install_queue = AsyncMock()
        self.retry_failed_chunks = AsyncMock(return_value=4)
        self.serve_http = AsyncMock()
        self.recall = AsyncMock(
            return_value=(Candidate(lane=Lane.Kind.FACTS, line="codec shipped"),),
        )
        self.ingest = AsyncMock(return_value=_DOC_ID)
        self.enqueue = AsyncMock()
        self.backup = AsyncMock(return_value=SimpleNamespace(bytes=7, path="/tmp/x.dump"))
        self.restore = AsyncMock(return_value=SimpleNamespace(path="/tmp/x.dump", database="aizk"))


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> Seams:
    seams = Seams()
    monkeypatch.setattr(cli.ops, "run_alembic", seams.run_alembic)
    monkeypatch.setattr(cli.ops, "alembic_config", seams.alembic_config)
    monkeypatch.setattr(cli.ops, "setup", seams.setup)
    monkeypatch.setattr(cli.ops, "scoped_rls_violations", seams.rls)
    monkeypatch.setattr(cli, "enable_spans", seams.enable_spans)
    monkeypatch.setattr(cli, "run_worker", seams.worker)
    monkeypatch.setattr(cli, "install_queue_schema", seams.install_queue)
    monkeypatch.setattr(cli, "retry_failed_chunks", seams.retry_failed_chunks)
    monkeypatch.setattr(cli, "recall", seams.recall)
    monkeypatch.setattr(cli, "ingest_text", seams.ingest)
    monkeypatch.setattr(cli, "enqueue_pending", seams.enqueue)
    monkeypatch.setattr(cli.backup_ops, "backup_database", seams.backup)
    monkeypatch.setattr(cli.backup_ops, "restore_database", seams.restore)
    monkeypatch.setattr(cli.AizkMCP.shared(), "run_http_async", seams.serve_http)
    return seams


def dispatch(tokens: list[str]) -> None:
    cli.app(tokens, exit_on_error=False, result_action="return_value")


_COMMANDS = [
    (
        "db migrate",
        ["db", "migrate"],
        "run_alembic",
        (cli.command.upgrade, "CONFIG", "head"),
        {"sql": False},
        "done",
    ),
    (
        "db migrate offline",
        ["db", "migrate", "--sql"],
        "run_alembic",
        (cli.command.upgrade, "CONFIG", "head"),
        {"sql": True},
        None,
    ),
    (
        "db makemigrations",
        ["db", "makemigrations", "add col"],
        "run_alembic",
        (cli.command.revision, "CONFIG"),
        {"message": "add col", "autogenerate": True},
        "done",
    ),
    ("db install-queue", ["db", "install-queue"], "install_queue", (), {}, "done"),
    (
        "db retry-failed-chunks",
        ["db", "retry-failed-chunks", "--limit", "7"],
        "retry_failed_chunks",
        (7,),
        {},
        "requeued 4 failed chunk jobs",
    ),
    (
        "db backup",
        ["db", "backup", "/tmp/x.dump"],
        "backup",
        ("/tmp/x.dump",),
        {},
        "backed up 7 bytes to /tmp/x.dump",
    ),
    (
        "db restore",
        ["db", "restore", "/tmp/x.dump"],
        "restore",
        ("/tmp/x.dump",),
        {},
        "restored /tmp/x.dump into aizk",
    ),
]


@pytest.mark.parametrize(
    ("tokens", "recorder_name", "expected_args", "expected_kwargs", "expected_output"),
    [case[1:] for case in _COMMANDS],
    ids=[case[0] for case in _COMMANDS],
)
def test_command_dispatches_argv_to_its_boundary(
    tokens: list[str],
    recorder_name: str,
    expected_args: tuple[Callable[..., None] | str | int, ...],
    expected_kwargs: dict[str, str | bool],
    expected_output: str | None,
    seams: Seams,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatch(tokens)
    recorder = cast("Mock", getattr(seams, recorder_name))
    out = capsys.readouterr().out
    assert recorder.call_count == 1
    assert recorder.call_args.args == expected_args
    assert recorder.call_args.kwargs == expected_kwargs
    assert (expected_output in out) if expected_output is not None else (out == "")


@pytest.mark.parametrize("profiling", [True, False])
def test_worker_enables_spans_only_when_profiling(
    seams: Seams, settings: Settings, monkeypatch: pytest.MonkeyPatch, profiling: bool
) -> None:
    monkeypatch.setattr(settings, "profiling", profiling)

    dispatch(["worker", "--batch-size", "7"])

    assert seams.worker.call_args.kwargs["batch_size"] == 7
    assert seams.enable_spans.call_count == int(profiling)


@pytest.mark.parametrize(
    ("tokens", "expected_query", "expected_user", "candidates", "expected_output"),
    [
        (
            ["recall-context", "hello world", "--k", "3", "--user", str(_OTHER_USER_ID)],
            "hello world",
            _OTHER_USER_ID,
            (Candidate(lane=Lane.Kind.FACTS, line="codec shipped"),),
            "codec shipped",
        ),
        (
            ["recall-context"],
            "recent decisions, patterns, gotchas, and project context",
            cli.settings.system_user_id,
            (Candidate(lane=Lane.Kind.FACTS, line="codec shipped"),),
            "codec shipped",
        ),
        (
            ["recall-context"],
            "recent decisions, patterns, gotchas, and project context",
            cli.settings.system_user_id,
            (),
            "no context recalled",
        ),
    ],
    ids=["explicit", "default", "empty"],
)
def test_recall_context_resolves_query_user_and_output(
    seams: Seams,
    capsys: pytest.CaptureFixture[str],
    tokens: list[str],
    expected_query: str,
    expected_user: UUID5 | UUID7,
    candidates: tuple[Candidate, ...],
    expected_output: str,
) -> None:
    seams.recall.return_value = candidates
    dispatch(tokens)

    assert seams.recall.call_args.args[0] == expected_query
    assert seams.recall.call_args.kwargs["user"] == User.system((expected_user,))
    assert expected_output in capsys.readouterr().out


@pytest.mark.parametrize("violations", [[], ["fact_claim: FORCE row level security missing"]])
def test_check_rls_gates_on_violations(
    seams: Seams, capsys: pytest.CaptureFixture[str], violations: list[str]
) -> None:
    seams.rls.return_value = violations

    if violations:
        with pytest.raises(SystemExit) as exit_info:
            dispatch(["db", "check-rls"])
        assert exit_info.value.code == 1
        assert violations[0] in capsys.readouterr().out
    else:
        dispatch(["db", "check-rls"])
        assert "ok" in capsys.readouterr().out


def test_check_public_reports_complete_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    dispatch(["check-public"])

    assert "configuration is complete" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command_name", "clean", "exits"),
    [("audit", True, False), ("audit", False, True), ("apply", True, False)],
    ids=["audit-clean", "audit-drift", "apply"],
)
def test_logto_commands_report_policy_and_close_the_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command_name: str,
    clean: bool,
    exits: bool,
) -> None:
    client = SimpleNamespace(close=AsyncMock())
    report = PolicyReport(clean=clean, changes=() if clean else ("repair role",))
    reconciler = SimpleNamespace(
        audit=AsyncMock(return_value=report),
        apply=AsyncMock(return_value=report),
    )
    monkeypatch.setattr(cli, "LogtoClient", Mock(return_value=client))
    monkeypatch.setattr(cli, "LogtoPolicy", Mock(return_value=reconciler))

    if exits:
        with pytest.raises(SystemExit) as exit_info:
            dispatch(["logto", command_name])
        assert exit_info.value.code == 1
    else:
        dispatch(["logto", command_name])

    assert getattr(reconciler, command_name).call_count == 1
    assert client.close.call_count == 1
    assert f'"clean": {str(clean).lower()}' in capsys.readouterr().out


@pytest.mark.parametrize("with_worker", [True, False])
@pytest.mark.parametrize("auto_setup", [True, False])
def test_serve_mcp_runs_http_and_optionally_the_worker(
    seams: Seams,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    with_worker: bool,
    auto_setup: bool,
) -> None:
    monkeypatch.setattr(settings, "mcp_port", 9999)
    monkeypatch.setattr(settings, "serve_with_worker", with_worker)
    monkeypatch.setattr(settings, "auto_setup", auto_setup)
    monkeypatch.setattr(settings, "profiling", with_worker)

    dispatch(["serve-mcp"])

    assert seams.serve_http.call_count == 1
    assert seams.serve_http.call_args.kwargs["port"] == 9999
    assert seams.worker.call_count == (1 if with_worker else 0)
    assert seams.setup.call_count == int(auto_setup)
    assert seams.enable_spans.call_count == int(with_worker)


@pytest.mark.parametrize("state", ["present", "unset", "missing-file"])
def test_capture_session_handles_present_and_missing_transcripts(
    seams: Seams,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    transcript = tmp_path / "session.jsonl"
    if state == "present":
        transcript.write_text("decided to ship the codec", encoding="utf-8")
        monkeypatch.setenv("AIZK_SESSION_TRANSCRIPT", str(transcript))
    elif state == "unset":
        monkeypatch.delenv("AIZK_SESSION_TRANSCRIPT", raising=False)
    else:
        monkeypatch.setenv("AIZK_SESSION_TRANSCRIPT", str(transcript))

    dispatch(["capture-session"])

    if state == "present":
        assert seams.ingest.call_args.args[0] == cli.User.system((cli.settings.system_user_id,))
        assert seams.ingest.call_args.args[1] == "decided to ship the codec"
        assert seams.ingest.call_args.kwargs["title"] == "session"
        assert seams.ingest.call_args.kwargs["created_by"] == cli.settings.system_user_id
        assert seams.enqueue.call_count == 1
        assert str(_DOC_ID) in capsys.readouterr().out
    else:
        assert seams.ingest.call_count == 0
        assert seams.enqueue.call_count == 0
        assert "no session transcript" in capsys.readouterr().out


_OPERATOR_COMMANDS = [
    (["graph", "rebuild", "--limit", "5"], "rebuild", (3, 7), "3 entities and 7 facts"),
    (["graph", "decay", "--half-life-days", "30"], "decay", 4, "archived 4"),
    (["graph", "reembed"], "reembed", 9, "re-embedded 9"),
    (["graph", "communities"], "communities", 3, "built 3 communities"),
    (["graph", "raptor"], "raptor", 2, "built 2 summaries"),
    (
        ["graph", "forget", "wrong note"],
        "forget",
        SimpleNamespace(claims=6, documents=["A", "B"]),
        "retracted 6 claims from 2 notes",
    ),
    (["data", "promote", str(_DOC_ID), "team"], "promote", 5, "promoted 5 document into team"),
    (["data", "ingest", "notes/"], "ingest", 4, "ingested 4 documents"),
    (["data", "ingest-image", "pic.png"], "ingest_image", _DOC_ID, str(_DOC_ID)),
    (["data", "export", "dump.jsonl"], "export_scope", Rendered("EXPORT-DUMP"), "EXPORT-DUMP"),
    (
        ["ontology", "define-entity", "Area", "a domain"],
        "define_entity_kind",
        None,
        "entity kind Area defined",
    ),
    (
        ["ontology", "define-relation", "funds", "x funds y"],
        "define_relation_kind",
        None,
        "relation kind funds defined",
    ),
    (
        ["db", "setup"],
        "setup",
        SimpleNamespace(migrated_from="abc123", migrated_to="def456"),
        "migrated abc123 -> def456",
    ),
]

type _CommandResult = int | UUID7 | tuple[int, int] | SimpleNamespace | Rendered | None


@pytest.mark.parametrize(
    ("tokens", "fn_name", "ret", "expected"),
    _OPERATOR_COMMANDS,
    ids=[" ".join(tokens[:2]) for tokens, _, _, _ in _OPERATOR_COMMANDS],
)
def test_operator_command_routes_to_admin_and_prints(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tokens: list[str],
    fn_name: str,
    ret: _CommandResult,
    expected: str,
) -> None:
    recorder = AsyncMock(return_value=ret)
    monkeypatch.setattr(cli.admin, fn_name, recorder)

    dispatch(tokens)

    assert recorder.call_count == 1
    assert expected in capsys.readouterr().out


def test_audit_renders_each_write_with_scopes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scope = uuid.UUID("44444444-4444-4444-4444-444444444444")
    docs = [
        SimpleNamespace(id=_DOC_ID, subject_type="project", scopes=[scope], title="Shared note"),
        SimpleNamespace(id=_USER_ID, subject_type=None, scopes=[], title=None),
    ]
    monkeypatch.setattr(cli.admin, "audit", AsyncMock(return_value=docs))

    dispatch(["data", "audit", "--limit", "5"])

    out = capsys.readouterr().out
    assert f"{_DOC_ID}  project  [{scope}]  Shared note" in out
    assert f"{_USER_ID}  source  [private]  -" in out


def test_list_ontology_marks_structural_kinds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        SimpleNamespace(kind="entity", name="Concept", domain="general", uses=3, structural=False),
        SimpleNamespace(
            kind="entity", name="RaptorSummary", domain="core", uses=1, structural=True
        ),
    ]
    monkeypatch.setattr(cli.admin, "list_ontology", AsyncMock(return_value=rows))

    dispatch(["ontology", "list"])

    out = capsys.readouterr().out.splitlines()
    concept = next(line for line in out if "Concept" in line)
    raptor = next(line for line in out if "RaptorSummary" in line)
    assert concept.startswith("  ") and "uses=3" in concept  # unmarked, extractable
    assert raptor.startswith("* ") and "uses=1" in raptor  # starred, structural


def test_profile_report_lists_spans_or_reports_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.admin, "profile_report", Mock(return_value=["span-one"]))
    dispatch(["profile-report"])
    assert "span-one" in capsys.readouterr().out

    monkeypatch.setattr(cli.admin, "profile_report", Mock(return_value=[]))
    dispatch(["profile-report"])
    assert "no spans recorded" in capsys.readouterr().out


class Jsonable:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    def model_dump_json(self, indent: int | None = None) -> str:
        return self.payload


@pytest.mark.parametrize(
    ("tokens", "fn_name"),
    [(["db", "tasks-status"], "tasks_status"), (["db", "health"], "health")],
    ids=["tasks-status", "health"],
)
def test_json_command_prints_the_model_dump(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tokens: list[str],
    fn_name: str,
) -> None:
    monkeypatch.setattr(cli.admin, fn_name, AsyncMock(return_value=Jsonable('{"ok": true}')))

    dispatch(tokens)

    assert '{"ok": true}' in capsys.readouterr().out


def test_database_reset_requires_the_exact_name_then_prints_the_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = AsyncMock(
        return_value=SimpleNamespace(database=cli.settings.db_name, migrated_to="0001_init"),
    )
    monkeypatch.setattr(cli.admin, "reset_database", recorder)

    with pytest.raises(ValueError, match="confirmation"):
        dispatch(["db", "reset", "wrong-database"])
    assert recorder.call_count == 0

    dispatch(["db", "reset", cli.settings.db_name])

    assert recorder.call_count == 1
    assert f"reset {cli.settings.db_name} at 0001_init" in capsys.readouterr().out
