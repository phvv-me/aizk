import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

import aizk.cli as cli
from aizk.config import Settings
from aizk.mcp import server as mcp_server
from aizk.store.identity import User

DOC_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
OTHER_USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


class Recorder:
    def __init__(self, ret: object = None, is_async: bool = False) -> None:
        self.ret = ret
        self.is_async = is_async
        self.args: tuple[object, ...] = ()
        self.kwargs: dict[str, object] = {}
        self.count = 0

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.args = args
        self.kwargs = kwargs
        self.count += 1
        if self.is_async:

            async def coro() -> object:
                return self.ret

            return coro()
        return self.ret


class Block:
    def __init__(self, lane: str, line: str) -> None:
        self.lane = lane
        self.line = line


class Rendered:
    def __init__(self, text: str) -> None:
        self.text = text

    def render(self) -> str:
        return self.text


class Seams:
    def __init__(self) -> None:
        self.run_alembic = Recorder()
        self.alembic_config = Recorder(ret="CONFIG")
        self.setup = Recorder(ret=SimpleNamespace(migrated_to="head"), is_async=True)
        self.rls = Recorder(ret=[], is_async=True)
        self.enable_spans = Recorder()
        self.worker = Recorder(is_async=True)
        self.install_queue = Recorder(is_async=True)
        self.serve_http = Recorder(is_async=True)
        self.recall = Recorder(ret=(Block("fact", "codec shipped"),), is_async=True)
        self.ingest = Recorder(ret=DOC_ID, is_async=True)
        self.enqueue = Recorder(is_async=True)
        self.run_scale = Recorder(ret=Rendered("SCALE-CURVE"), is_async=True)
        self.backup = Recorder(ret=SimpleNamespace(bytes=7, path="/tmp/x.dump"), is_async=True)
        self.restore = Recorder(
            ret=SimpleNamespace(path="/tmp/x.dump", database="aizk"), is_async=True
        )


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
    monkeypatch.setattr(cli, "recall", seams.recall)
    monkeypatch.setattr(cli, "ingest_text", seams.ingest)
    monkeypatch.setattr(cli, "enqueue_pending", seams.enqueue)
    monkeypatch.setattr(cli.backup_ops, "backup_database", seams.backup)
    monkeypatch.setattr(cli.backup_ops, "restore_database", seams.restore)
    monkeypatch.setattr(mcp_server.server, "run_http_async", seams.serve_http)
    monkeypatch.setattr(cli.admin, "scale", seams.run_scale)
    return seams


def dispatch(tokens: list[str]) -> None:
    cli.app(tokens, exit_on_error=False, result_action="return_value")


def check_migrate(seams: Seams, out: str) -> None:
    assert seams.run_alembic.args == (cli.command.upgrade, "CONFIG", "head")
    assert "done" in out


def check_offline_migrate(seams: Seams, out: str) -> None:
    assert seams.run_alembic.args == (cli.command.upgrade, "CONFIG", "head")
    assert seams.run_alembic.kwargs == {"sql": True}
    assert "done" not in out


def check_makemigrations(seams: Seams, out: str) -> None:
    assert seams.run_alembic.args == (cli.command.revision, "CONFIG")
    assert seams.run_alembic.kwargs == {"message": "add col", "autogenerate": True}
    assert "done" in out


def check_install_queue(seams: Seams, out: str) -> None:
    assert seams.install_queue.count == 1
    assert "done" in out


def check_scale(seams: Seams, out: str) -> None:
    assert seams.run_scale.kwargs["sizes"] == (1, 2)
    assert seams.run_scale.kwargs["k"] == 4
    assert seams.run_scale.kwargs["recall_p95_ms"] == 50.0
    assert "SCALE-CURVE" in out


def check_backup(seams: Seams, out: str) -> None:
    assert seams.backup.args == ("/tmp/x.dump",)
    assert "backed up 7 bytes to /tmp/x.dump" in out


def check_restore(seams: Seams, out: str) -> None:
    assert seams.restore.args == ("/tmp/x.dump",)
    assert "restored /tmp/x.dump into aizk" in out


COMMANDS: list[tuple[str, list[str], Callable[[Seams, str], None]]] = [
    ("db migrate", ["db", "migrate"], check_migrate),
    ("db migrate offline", ["db", "migrate", "--sql"], check_offline_migrate),
    ("db makemigrations", ["db", "makemigrations", "add col"], check_makemigrations),
    ("db install-queue", ["db", "install-queue"], check_install_queue),
    (
        "eval scale",
        ["eval", "scale", "--sizes", "1,2", "--k", "4", "--recall-p95-ms", "50"],
        check_scale,
    ),
    ("db backup", ["db", "backup", "/tmp/x.dump"], check_backup),
    ("db restore", ["db", "restore", "/tmp/x.dump"], check_restore),
]


@pytest.mark.parametrize(
    ("tokens", "check"),
    [(tokens, check) for _, tokens, check in COMMANDS],
    ids=[name for name, _, _ in COMMANDS],
)
def test_command_dispatches_argv_to_its_boundary(
    tokens: list[str],
    check: Callable[[Seams, str], None],
    seams: Seams,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatch(tokens)
    check(seams, capsys.readouterr().out)


@pytest.mark.parametrize("profiling", [True, False])
def test_worker_enables_spans_only_when_profiling(
    seams: Seams, settings: Settings, monkeypatch: pytest.MonkeyPatch, profiling: bool
) -> None:
    monkeypatch.setattr(settings, "profiling", profiling)

    dispatch(["worker", "--batch-size", "7"])

    assert seams.worker.kwargs["batch_size"] == 7
    assert seams.enable_spans.count == int(profiling)


@pytest.mark.parametrize(
    ("tokens", "expected_query", "expected_user"),
    [
        (
            ["recall-context", "hello world", "--k", "3", "--user", str(OTHER_USER_ID)],
            "hello world",
            OTHER_USER_ID,
        ),
        (
            ["recall-context"],
            "recent decisions, patterns, gotchas, and project context",
            cli.settings.system_user_id,
        ),
    ],
    ids=["explicit", "default"],
)
def test_recall_context_resolves_query_and_user(
    seams: Seams,
    capsys: pytest.CaptureFixture[str],
    tokens: list[str],
    expected_query: str,
    expected_user: uuid.UUID,
) -> None:
    dispatch(tokens)

    assert seams.recall.args[0] == expected_query
    assert seams.recall.kwargs["user"] == User.system((expected_user,))
    assert "[fact] codec shipped" in capsys.readouterr().out


def test_recall_context_prints_placeholder_when_nothing_recalled(
    seams: Seams, capsys: pytest.CaptureFixture[str]
) -> None:
    seams.recall.ret = ()

    dispatch(["recall-context"])

    assert "no context recalled" in capsys.readouterr().out


@pytest.mark.parametrize("violations", [[], ["fact_claim: FORCE row level security missing"]])
def test_check_rls_gates_on_violations(
    seams: Seams, capsys: pytest.CaptureFixture[str], violations: list[str]
) -> None:
    seams.rls.ret = violations

    if violations:
        with pytest.raises(SystemExit) as exit_info:
            dispatch(["db", "check-rls"])
        assert exit_info.value.code == 1
        assert violations[0] in capsys.readouterr().out
    else:
        dispatch(["db", "check-rls"])
        assert "ok" in capsys.readouterr().out


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

    assert seams.serve_http.count == 1
    assert seams.serve_http.kwargs["port"] == 9999
    assert seams.worker.count == (1 if with_worker else 0)
    assert seams.setup.count == int(auto_setup)
    assert seams.enable_spans.count == int(with_worker)


def test_capture_session_ingests_the_transcript_and_enqueues_extraction(
    seams: Seams,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("decided to ship the codec", encoding="utf-8")
    monkeypatch.setenv("AIZK_SESSION_TRANSCRIPT", str(transcript))

    dispatch(["capture-session"])

    assert seams.ingest.args[0] == cli.User.system((cli.settings.system_user_id,))
    assert seams.ingest.args[1] == "decided to ship the codec"
    assert seams.ingest.kwargs["title"] == "session"
    assert seams.ingest.kwargs["created_by"] == cli.settings.system_user_id
    assert seams.enqueue.count == 1
    assert str(DOC_ID) in capsys.readouterr().out


@pytest.mark.parametrize("state", ["unset", "missing-file"])
def test_capture_session_is_a_quiet_noop_without_a_transcript(
    seams: Seams,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    if state == "unset":
        monkeypatch.delenv("AIZK_SESSION_TRANSCRIPT", raising=False)
    else:
        monkeypatch.setenv("AIZK_SESSION_TRANSCRIPT", str(tmp_path / "absent.jsonl"))

    dispatch(["capture-session"])

    assert seams.ingest.count == 0
    assert seams.enqueue.count == 0
    assert "no session transcript" in capsys.readouterr().out


OPERATOR_COMMANDS: list[tuple[list[str], str, object, str]] = [
    (["graph", "rebuild", "--limit", "5"], "rebuild", (3, 7), "3 entities and 7 facts"),
    (["graph", "decay", "--half-life-days", "30"], "decay", 4, "archived 4"),
    (["graph", "reembed"], "reembed", 9, "re-embedded 9"),
    (["graph", "raptor"], "raptor", 2, "built 2 summaries"),
    (
        ["graph", "forget", "wrong note"],
        "forget",
        SimpleNamespace(claims=6, documents=["A", "B"]),
        "retracted 6 claims from 2 notes",
    ),
    (["data", "promote", str(DOC_ID), "team"], "promote", 5, "promoted 5 document into team"),
    (["data", "ingest", "notes/"], "ingest", 4, "ingested 4 documents"),
    (["data", "ingest-image", "pic.png"], "ingest_image", DOC_ID, str(DOC_ID)),
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
    (["eval", "bench"], "bench", Rendered("BENCH-REPORT"), "BENCH-REPORT"),
    (["eval", "sweep"], "sweep", Rendered("SWEEP-REPORT"), "SWEEP-REPORT"),
    (["eval", "plans"], "plan_study", Rendered("PLAN-REPORT"), "PLAN-REPORT"),
    (["eval", "gate"], "gate_check", Rendered("GATE-REPORT"), "GATE-REPORT"),
    (["eval", "groupmem", "corpus/"], "groupmem", Rendered("GM-REPORT"), "GM-REPORT"),
]


@pytest.mark.parametrize(
    ("tokens", "fn_name", "ret", "expected"),
    OPERATOR_COMMANDS,
    ids=[" ".join(tokens[:2]) for tokens, _, _, _ in OPERATOR_COMMANDS],
)
def test_operator_command_routes_to_admin_and_prints(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tokens: list[str],
    fn_name: str,
    ret: object,
    expected: str,
) -> None:
    recorder = Recorder(ret=ret, is_async=True)
    monkeypatch.setattr(cli.admin, fn_name, recorder)

    dispatch(tokens)

    assert recorder.count == 1
    assert expected in capsys.readouterr().out


class JsonRendered(Rendered):
    def model_dump_json(self, indent: int | None = None) -> str:
        return '{"kind": "study"}'


@pytest.mark.parametrize(
    ("tokens", "fn_name", "expected_kwargs"),
    [
        (
            ["eval", "plans", "--strata", "local, multihop", "--gate-limit", "9"],
            "plan_study",
            {
                "k": 8,
                "per_stratum": 8,
                "strata": ("local", "multihop"),
                "seeding": True,
                "gate_limit": 9,
            },
        ),
        (["eval", "gate", "--limit", "3"], "gate_check", {"limit": 3, "user_id": None}),
    ],
    ids=["plans", "gate"],
)
def test_eval_study_commands_write_the_json_report_beside_the_table(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    tokens: list[str],
    fn_name: str,
    expected_kwargs: dict[str, object],
) -> None:
    recorder = Recorder(ret=JsonRendered("STUDY-TABLE"), is_async=True)
    monkeypatch.setattr(cli.admin, fn_name, recorder)
    out_path = tmp_path / "report.json"

    dispatch([*tokens, "--out", str(out_path)])

    assert recorder.kwargs == expected_kwargs
    assert out_path.read_text(encoding="utf-8") == '{"kind": "study"}'
    printed = capsys.readouterr().out
    assert f"wrote {out_path}" in printed
    assert "STUDY-TABLE" in printed


def test_audit_renders_each_write_with_scopes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scope = uuid.UUID("44444444-4444-4444-4444-444444444444")
    docs = [
        SimpleNamespace(id=DOC_ID, kind="note", scopes=[scope], title="Shared note"),
        SimpleNamespace(id=USER_ID, kind="code", scopes=[], title=None),
    ]
    monkeypatch.setattr(cli.admin, "audit", Recorder(ret=docs, is_async=True))

    dispatch(["data", "audit", "--limit", "5"])

    out = capsys.readouterr().out
    assert f"{DOC_ID}  note  [{scope}]  Shared note" in out
    assert f"{USER_ID}  code  [private]  -" in out


def test_list_ontology_marks_structural_kinds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        SimpleNamespace(kind="entity", name="Concept", domain="general", uses=3, structural=False),
        SimpleNamespace(
            kind="entity", name="RaptorSummary", domain="core", uses=1, structural=True
        ),
    ]
    monkeypatch.setattr(cli.admin, "list_ontology", Recorder(ret=rows, is_async=True))

    dispatch(["ontology", "list"])

    out = capsys.readouterr().out.splitlines()
    concept = next(line for line in out if "Concept" in line)
    raptor = next(line for line in out if "RaptorSummary" in line)
    assert concept.startswith("  ") and "uses=3" in concept  # unmarked, extractable
    assert raptor.startswith("* ") and "uses=1" in raptor  # starred, structural


def test_profile_report_lists_spans_or_reports_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.admin, "profile_report", Recorder(ret=["span-one"]))
    dispatch(["profile-report"])
    assert "span-one" in capsys.readouterr().out

    monkeypatch.setattr(cli.admin, "profile_report", Recorder(ret=[]))
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
    monkeypatch.setattr(cli.admin, fn_name, Recorder(ret=Jsonable('{"ok": true}'), is_async=True))

    dispatch(tokens)

    assert '{"ok": true}' in capsys.readouterr().out
