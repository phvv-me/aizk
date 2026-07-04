import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import dbutil
import pytest

import aizk.cli as cli
from aizk.config import Settings
from aizk.eval import scale as eval_scale
from aizk.mcp import server as mcp_server
from aizk.store import Principal, acting_as

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
        self.serve = Recorder()
        self.recall = Recorder(ret=Pack([Block("fact", "codec shipped")]), is_async=True)
        self.ingest = Recorder(ret=DOC_ID, is_async=True)
        self.enqueue = Recorder(is_async=True)
        self.run_scale = Recorder(ret=Rendered("SCALE-CURVE"), is_async=True)
        self.create_principal = Recorder(ret=SimpleNamespace(id=USER_ID), is_async=True)


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
    monkeypatch.setattr(cli, "create_user_principal", seams.create_principal)
    monkeypatch.setattr(mcp_server.server, "run", seams.serve)
    monkeypatch.setattr(eval_scale, "run_scale_benchmark", seams.run_scale)
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
    budget = seams.run_scale.kwargs["budget"]
    assert isinstance(budget, eval_scale.Budget) and budget.recall_p95_ms == 50.0
    assert "SCALE-CURVE" in out


def check_create_user(seams: Seams, out: str) -> None:
    assert seams.create_principal.args[0] == "alice"
    assert str(USER_ID) in out


COMMANDS: list[tuple[str, list[str], Callable[[Seams, str], None]]] = [
    ("migrate", ["migrate"], check_migrate),
    ("makemigrations", ["makemigrations", "add col"], check_makemigrations),
    ("install-queue", ["install-queue"], check_install_queue),
    ("scale", ["scale", "--sizes", "1,2", "--k", "4", "--recall-p95-ms", "50"], check_scale),
    ("create-user", ["create-user", "alice"], check_create_user),
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
        (["recall-context"], cli.PROJECT_CONTEXT_QUERY, cli.settings.system_principal_id),
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
            dispatch(["check-rls"])
        assert exit_info.value.code == 1
        assert violations[0] in capsys.readouterr().out
    else:
        dispatch(["check-rls"])
        assert "ok" in capsys.readouterr().out


@pytest.mark.parametrize("over_http", [True, False])
def test_serve_mcp_selects_the_transport_on_the_http_flag(
    seams: Seams, settings: Settings, monkeypatch: pytest.MonkeyPatch, over_http: bool
) -> None:
    """serve-mcp runs the module server over http when mcp_http is set, plain stdio otherwise."""
    monkeypatch.setattr(settings, "mcp_http", over_http)
    monkeypatch.setattr(settings, "mcp_port", 9999)

    dispatch(["serve-mcp"])

    assert seams.serve.count == 1
    assert bool(seams.serve.kwargs) == over_http  # http carries transport kwargs, stdio runs bare
    if over_http:
        assert seams.serve.kwargs["transport"] == "http"
        assert seams.serve.kwargs["port"] == 9999


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
    assert seams.ingest.kwargs["owner_id"] == cli.settings.system_principal_id
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


def test_create_user_principal_mints_a_row_through_one_system_session(migrated_db: None) -> None:
    """The CLI's own seam opens a system-acting session and mints a readable principal in it.

    Runs against the live schema so `system_session`, `Principal.create`, and the commit all run
    for real, then reads the row back to prove it landed with the given display name.
    """

    async def run() -> tuple[Principal, str | None]:
        await dbutil.reset_db()
        await dbutil.seed_principal(cli.settings.system_principal_id)
        created = await cli.create_user_principal("alice")
        async with acting_as(cli.settings.system_principal_id) as session:
            reloaded = await session.get(Principal, created.id)
        assert reloaded is not None
        return created, reloaded.display_name

    created, display_name = dbutil.run(run())

    assert display_name == "alice"
    assert created.display_name == "alice"
