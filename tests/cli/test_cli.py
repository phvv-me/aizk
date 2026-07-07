import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

import aizk.cli as cli
from aizk.config import Settings
from aizk.mcp import server as mcp_server

DOC_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
PRINCIPAL_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


class Recorder:
    """A call double recording the last call's arguments and returning a fixed value.

    It stands over one boundary the CLI reaches, capturing the argv-derived arguments so a test
    asserts the wiring and handing back a fresh coroutine when the boundary is awaited.

    ret: value returned, awaited through a new coroutine when `is_async`.
    is_async: whether a call returns an awaitable rather than the value directly.
    """

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
    """One recalled context block, the lane-tagged line `recall-context` formats to stdout."""

    def __init__(self, lane: str, line: str) -> None:
        self.lane = lane
        self.line = line


class Pack:
    """A stand-in context pack whose `blocks` are the lines `recall-context` prints."""

    def __init__(self, blocks: list[Block]) -> None:
        self.blocks = blocks


class Rendered:
    """A stand-in report whose `render()` is the one text the scale verb prints.

    text: fixed string `render()` returns, so a test reads stdout without a real report.
    """

    def __init__(self, text: str) -> None:
        self.text = text

    def render(self) -> str:
        return self.text


class Seams:
    """The recording doubles installed over every boundary a CLI command reaches."""

    def __init__(self) -> None:
        self.run_alembic = Recorder()
        self.alembic_config = Recorder(ret="CONFIG")
        self.rls = Recorder(ret=[], is_async=True)
        self.enable_spans = Recorder()
        self.worker = Recorder(is_async=True)
        self.install_queue = Recorder(is_async=True)
        self.serve_http = Recorder(is_async=True)
        self.serve_stdio = Recorder(is_async=True)
        self.recall = Recorder(ret=Pack([Block("fact", "codec shipped")]), is_async=True)
        self.ingest = Recorder(ret=DOC_ID, is_async=True)
        self.enqueue = Recorder(is_async=True)
        self.run_scale = Recorder(ret=Rendered("SCALE-CURVE"), is_async=True)
        self.create_principal = Recorder(ret=SimpleNamespace(id=USER_ID), is_async=True)
        self.backup = Recorder(ret=SimpleNamespace(bytes=7, path="/tmp/x.dump"), is_async=True)
        self.restore = Recorder(
            ret=SimpleNamespace(path="/tmp/x.dump", database="aizk"), is_async=True
        )


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> Seams:
    """Install recording doubles over the alembic, queue, worker, serve, recall, and scale seams.

    Each command reads the real global `settings` while every boundary call is captured, so a test
    asserts the argv it was handed without a database, network, or server. The `ops` module
    functions the thin commands delegate to are patched on `ops` itself, the lazily-imported
    `run_scale_benchmark` and mcp `server` on their own modules.
    """
    seams = Seams()
    monkeypatch.setattr(cli.ops, "run_alembic", seams.run_alembic)
    monkeypatch.setattr(cli.ops, "alembic_config", seams.alembic_config)
    monkeypatch.setattr(cli.ops, "scoped_rls_violations", seams.rls)
    monkeypatch.setattr(cli, "enable_spans", seams.enable_spans)
    monkeypatch.setattr(cli, "run_worker", seams.worker)
    monkeypatch.setattr(cli, "install_queue_schema", seams.install_queue)
    monkeypatch.setattr(cli, "assemble_context_pack", seams.recall)
    monkeypatch.setattr(cli, "ingest_text", seams.ingest)
    monkeypatch.setattr(cli, "enqueue_pending", seams.enqueue)
    monkeypatch.setattr(cli.admin, "create_user", seams.create_principal)
    monkeypatch.setattr(cli.backup_ops, "backup_database", seams.backup)
    monkeypatch.setattr(cli.backup_ops, "restore_database", seams.restore)
    monkeypatch.setattr(mcp_server.server, "run_http_async", seams.serve_http)
    monkeypatch.setattr(mcp_server.server, "run_stdio_async", seams.serve_stdio)
    monkeypatch.setattr(cli.admin, "scale", seams.run_scale)
    return seams


def dispatch(tokens: list[str]) -> None:
    """Drive the cyclopts app over an argv list without exiting the process on a command error."""
    cli.app(tokens, exit_on_error=False, result_action="return_value")


def check_migrate(seams: Seams, out: str) -> None:
    assert seams.run_alembic.args == (cli.command.upgrade, "CONFIG", "head")
    assert "done" in out


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


def check_create_user(seams: Seams, out: str) -> None:
    assert seams.create_principal.args[0] == "alice"
    assert str(USER_ID) in out


def check_backup(seams: Seams, out: str) -> None:
    assert seams.backup.args == ("/tmp/x.dump",)
    assert "backed up 7 bytes to /tmp/x.dump" in out


def check_restore(seams: Seams, out: str) -> None:
    assert seams.restore.args == ("/tmp/x.dump",)
    assert "restored /tmp/x.dump into aizk" in out


COMMANDS: list[tuple[str, list[str], Callable[[Seams, str], None]]] = [
    ("db migrate", ["db", "migrate"], check_migrate),
    ("db makemigrations", ["db", "makemigrations", "add col"], check_makemigrations),
    ("db install-queue", ["db", "install-queue"], check_install_queue),
    (
        "eval scale",
        ["eval", "scale", "--sizes", "1,2", "--k", "4", "--recall-p95-ms", "50"],
        check_scale,
    ),
    ("user create", ["user", "create", "alice"], check_create_user),
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
    """Each verb routes its argv to the right boundary with the right arguments and prints it.

    tokens: the argv the CLI dispatches, one row per command in the table.
    check: the per-command assertion over the captured call and stdout.
    """
    dispatch(tokens)
    check(seams, capsys.readouterr().out)


@pytest.mark.parametrize("profiling", [True, False])
def test_worker_enables_spans_only_when_profiling(
    seams: Seams, settings: Settings, monkeypatch: pytest.MonkeyPatch, profiling: bool
) -> None:
    """The worker drives the run loop with the given batch size and profiles only when asked."""
    monkeypatch.setattr(settings, "profiling", profiling)

    dispatch(["worker", "--batch-size", "7"])

    assert seams.worker.kwargs["batch_size"] == 7
    assert seams.enable_spans.count == int(profiling)


@pytest.mark.parametrize(
    ("tokens", "expected_query", "expected_principal"),
    [
        (
            ["recall-context", "hello world", "--k", "3", "--principal", str(PRINCIPAL_ID)],
            "hello world",
            PRINCIPAL_ID,
        ),
        (["recall-context"], cli.PROJECT_CONTEXT_QUERY, cli.settings.system_user_id),
    ],
    ids=["explicit", "default"],
)
def test_recall_context_resolves_query_and_principal(
    seams: Seams,
    capsys: pytest.CaptureFixture[str],
    tokens: list[str],
    expected_query: str,
    expected_principal: uuid.UUID,
) -> None:
    """An explicit query and principal pass through, a bare call falls back to the defaults.

    tokens: the argv the recall command dispatches.
    expected_query: the recall query the fallback resolves to.
    expected_principal: the principal id the fallback resolves to.
    """
    dispatch(tokens)

    assert seams.recall.args[0] == expected_query
    assert seams.recall.kwargs["principal_id"] == expected_principal
    assert "[fact] codec shipped" in capsys.readouterr().out


def test_recall_context_prints_placeholder_when_nothing_recalled(
    seams: Seams, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty pack prints the no-context placeholder rather than a blank line."""
    seams.recall.ret = Pack([])

    dispatch(["recall-context"])

    assert "no context recalled" in capsys.readouterr().out


@pytest.mark.parametrize("violations", [[], ["fact_claim: FORCE row level security missing"]])
def test_check_rls_gates_on_violations(
    seams: Seams, capsys: pytest.CaptureFixture[str], violations: list[str]
) -> None:
    """A clean schema prints ok, a lost policy is listed and gates CI through a non-zero exit."""
    seams.rls.ret = violations

    if violations:
        with pytest.raises(SystemExit) as exit_info:
            dispatch(["db", "check-rls"])
        assert exit_info.value.code == 1
        assert violations[0] in capsys.readouterr().out
    else:
        dispatch(["db", "check-rls"])
        assert "ok" in capsys.readouterr().out


@pytest.mark.parametrize("over_http", [True, False])
@pytest.mark.parametrize("with_worker", [True, False])
def test_serve_mcp_gathers_the_transport_and_optionally_the_worker(
    seams: Seams,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    over_http: bool,
    with_worker: bool,
) -> None:
    """serve-mcp runs http or stdio, gathering the worker on one loop when serve_with_worker."""
    monkeypatch.setattr(settings, "mcp_http", over_http)
    monkeypatch.setattr(settings, "mcp_port", 9999)
    monkeypatch.setattr(settings, "serve_with_worker", with_worker)

    dispatch(["serve-mcp"])

    if over_http:
        assert seams.serve_http.count == 1 and seams.serve_stdio.count == 0
        assert seams.serve_http.kwargs["port"] == 9999
    else:
        assert seams.serve_stdio.count == 1 and seams.serve_http.count == 0
    assert seams.worker.count == (1 if with_worker else 0)


def test_capture_session_ingests_the_transcript_and_enqueues_extraction(
    seams: Seams,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With a transcript on disk the Stop hook remembers its text and enqueues the graph build."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("decided to ship the codec", encoding="utf-8")
    monkeypatch.setenv(cli.TRANSCRIPT_ENV, str(transcript))

    dispatch(["capture-session"])

    assert seams.ingest.args[0] == "decided to ship the codec"
    assert seams.ingest.kwargs["title"] == "session"
    assert seams.ingest.kwargs["owner_id"] == cli.settings.system_user_id
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
    """With no transcript, env unset or pointing nowhere, the hook neither ingests nor enqueues."""
    if state == "unset":
        monkeypatch.delenv(cli.TRANSCRIPT_ENV, raising=False)
    else:
        monkeypatch.setenv(cli.TRANSCRIPT_ENV, str(tmp_path / "absent.jsonl"))

    dispatch(["capture-session"])

    assert seams.ingest.count == 0
    assert seams.enqueue.count == 0
    assert "no session transcript" in capsys.readouterr().out


# each operator command routes its argv to the matching `admin.<fn>` and prints a summary. The
# tuple is (argv, admin function name, its faked return, a substring the command must print).
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
    (["data", "promote", str(DOC_ID), "team"], "promote", 5, "promoted 5 rows into team"),
    (["data", "ingest", "notes/"], "ingest", 4, "ingested 4 documents"),
    (
        ["user", "link", "gh|42", "--name", "Al"],
        "link_user",
        SimpleNamespace(id=USER_ID),
        str(USER_ID),
    ),
    (["group", "create", "team"], "create_group", SimpleNamespace(id=DOC_ID), str(DOC_ID)),
    (["group", "add-member", str(USER_ID), "team"], "add_member", None, "joined team"),
    (["group", "publish", "team"], "publish_group", None, "public=True"),
    (["group", "delete", "team"], "delete_group", None, "team deleted"),
    (
        ["ontology", "define-entity", "Area", "a domain"],
        "define_entity_kind",
        None,
        "entity kind Area defined",
    ),
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
    """Every operator verb delegates to its `admin` function and renders a readable summary.

    The whole operational surface lives in the CLI now, so this is the operator plane's own wiring
    contract: the argv reaches the matching `admin.<fn>` and its result prints, no MCP tool
    involved.

    tokens: the argv the CLI dispatches.
    fn_name: the `admin` function the command must delegate to.
    ret: the value the faked admin function resolves to.
    expected: a substring the command must print from that result.
    """
    recorder = Recorder(ret=ret, is_async=True)
    monkeypatch.setattr(cli.admin, fn_name, recorder)

    dispatch(tokens)

    assert recorder.count == 1
    assert expected in capsys.readouterr().out


def test_list_groups_renders_each_group_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The roster command prints one line per group with its visibility and member count."""
    rows = [
        {"name": "team", "public": True, "members": 3},
        {"name": "vault", "public": False, "members": 1},
    ]
    monkeypatch.setattr(cli.admin, "list_groups", Recorder(ret=rows, is_async=True))

    dispatch(["group", "list"])

    out = capsys.readouterr().out
    assert "team  public  3 members" in out
    assert "vault  members-only  1 members" in out
