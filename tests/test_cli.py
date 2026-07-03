import asyncio
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

import aizk.cli as cli
from aizk.config import Settings
from aizk.eval import scale as eval_scale
from aizk.mcp import server as mcp_server

DOC_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


class Recorder:
    """A call double that captures the last call's args and returns a fixed value.

    It stands in for one external boundary the CLI invokes, recording argv-derived arguments so a
    test asserts the wiring, and returning a coroutine when async so `asyncio.run` can await it.

    ret: the value handed back, awaited through a fresh coroutine when async.
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


class Rendered:
    """A stand-in for a report whose `render()` is the CLI's one text-producing seam.

    text: the fixed string `render()` returns, so a test asserts on stdout without a real report.
    """

    def __init__(self, text: str) -> None:
        self.text = text

    def render(self) -> str:
        return self.text


class Seams:
    """The recording doubles installed over every external boundary the CLI commands reach."""

    def __init__(self) -> None:
        self.migrate = Recorder()
        self.revision = Recorder()
        self.rls = Recorder(ret=[], is_async=True)
        self.worker = Recorder(is_async=True)
        self.install_queue = Recorder(is_async=True)
        self.serve = Recorder()
        self.recall = Recorder(ret=Rendered("FORMATTED-RECALL"), is_async=True)
        self.ingest = Recorder(ret=DOC_ID, is_async=True)
        self.enqueue = Recorder(is_async=True)
        self.run_scale = Recorder(ret=Rendered("SCALE-CURVE"), is_async=True)
        self.create_principal = Recorder(ret=SimpleNamespace(id=USER_ID), is_async=True)


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> Seams:
    """Install recording doubles over the engine, queue, auth, and serve boundaries the CLI calls.

    Each command reads the real global `settings`, built once from the ambient env, while each
    migrate, recall, ingest, serve, and auth call is captured so a test asserts the argv it was
    handed without a database, network, or server. `migrate` is seamed at the alembic boundary
    itself, `command.upgrade`, since the command now runs it inline with no wrapper of our own to
    patch.
    """
    s = Seams()
    monkeypatch.setattr(cli.command, "upgrade", s.migrate)
    monkeypatch.setattr(cli.command, "revision", s.revision)
    monkeypatch.setattr(cli, "scoped_rls_violations", s.rls)
    monkeypatch.setattr(cli, "run_worker", s.worker)
    monkeypatch.setattr(cli, "install_queue_schema", s.install_queue)
    monkeypatch.setattr(cli, "recall", s.recall)
    monkeypatch.setattr(cli, "ingest_text", s.ingest)
    monkeypatch.setattr(cli, "enqueue_pending", s.enqueue)
    monkeypatch.setattr(cli, "create_user_principal", s.create_principal)
    monkeypatch.setattr(mcp_server.server, "run", s.serve)
    monkeypatch.setattr(eval_scale, "run_scale_benchmark", s.run_scale)
    return s


def check_migrate(s: Seams, out: str) -> None:
    assert s.migrate.count == 1
    assert "done" in out


def check_rls_ok(s: Seams, out: str) -> None:
    assert s.rls.count == 1
    assert "ok" in out


def check_worker(s: Seams, out: str) -> None:
    assert s.worker.count == 1
    assert s.worker.kwargs["batch_size"] == 7


def check_install_queue(s: Seams, out: str) -> None:
    assert s.install_queue.count == 1
    assert "done" in out


def check_recall_context(s: Seams, out: str) -> None:
    assert s.recall.args[0] == "hello world"
    assert s.recall.kwargs["k"] == 3
    assert "FORMATTED-RECALL" in out


def check_recall_default_query(s: Seams, out: str) -> None:
    assert s.recall.args[0] == cli.PROJECT_CONTEXT_QUERY
    assert "FORMATTED-RECALL" in out


def check_scale(s: Seams, out: str) -> None:
    assert s.run_scale.kwargs["sizes"] == (1, 2)
    assert s.run_scale.kwargs["k"] == 4
    assert "SCALE-CURVE" in out


def check_create_user(s: Seams, out: str) -> None:
    assert s.create_principal.args[0] == "alice"
    assert str(USER_ID) in out


def test_create_user_principal_opens_one_system_session_and_creates_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI's own seam opens exactly one system-acting session and mints the principal in it."""
    created = SimpleNamespace(id=USER_ID)
    seen: dict[str, object] = {}

    class FakeSystemSession:
        async def __aenter__(self) -> str:
            return "fake-session"

        async def __aexit__(self, *exc: object) -> bool:
            return False

    async def fake_create(session: object, name: str) -> SimpleNamespace:
        seen["session"] = session
        seen["name"] = name
        return created

    monkeypatch.setattr(cli, "system_session", lambda: FakeSystemSession())
    monkeypatch.setattr(cli.Principal, "create", fake_create)

    principal = asyncio.run(cli.create_user_principal("alice"))

    assert principal is created
    assert seen == {"session": "fake-session", "name": "alice"}


def check_makemigrations(s: Seams, out: str) -> None:
    assert s.revision.count == 1
    assert s.revision.kwargs["message"] == "add col"
    assert s.revision.kwargs["autogenerate"] is True
    assert "done" in out


COMMANDS: list[tuple[list[str], Callable[[Seams, str], None]]] = [
    (["migrate"], check_migrate),
    (["makemigrations", "add col"], check_makemigrations),
    (["check-rls"], check_rls_ok),
    (["worker", "--batch-size", "7"], check_worker),
    (["install-queue"], check_install_queue),
    (["recall-context", "hello world", "--k", "3"], check_recall_context),
    (["recall-context"], check_recall_default_query),
    (["scale", "--sizes", "1,2", "--k", "4"], check_scale),
    (["create-user", "alice"], check_create_user),
]


@pytest.mark.parametrize(
    ("tokens", "check"),
    COMMANDS,
    ids=[tokens[0] if len(tokens) == 1 else f"{tokens[0]}-{tokens[1]}" for tokens, _ in COMMANDS],
)
def test_command_dispatches_argv_to_its_boundary(
    tokens: list[str],
    check: Callable[[Seams, str], None],
    seams: Seams,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each verb routes its argv to the right call with the right arguments and prints its result.

    tokens: the argv the CLI dispatches, one row per command in the table.
    check: the per-command assertion over the captured call and stdout.
    """
    cli.app(tokens, exit_on_error=False, result_action="return_value")
    check(seams, capsys.readouterr().out)


def test_check_rls_exits_nonzero_and_lists_each_violation(
    seams: Seams, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scoped table that lost a policy is printed and gates CI through a non-zero exit."""
    seams.rls.ret = ["facts: FORCE row level security missing"]

    with pytest.raises(SystemExit) as exit_info:
        cli.app(["check-rls"], exit_on_error=False)

    assert exit_info.value.code == 1
    assert "FORCE row level security missing" in capsys.readouterr().out


def test_capture_session_ingests_the_transcript_and_enqueues_extraction(
    seams: Seams,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With a transcript on disk the hook remembers its text and enqueues the graph build."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("decided to ship the codec", encoding="utf-8")
    monkeypatch.setenv(cli.TRANSCRIPT_ENV, str(transcript))

    cli.app(["capture-session"], exit_on_error=False, result_action="return_value")

    assert seams.ingest.args[0] == "decided to ship the codec"
    assert seams.ingest.kwargs["title"] == "session"
    assert seams.enqueue.count == 1
    assert str(DOC_ID) in capsys.readouterr().out


class FakeConnection:
    """A double for the admin connection whose run_sync calls the catalog check synchronously."""

    async def run_sync(self, fn: Callable[[object], list[str]]) -> list[str]:
        return fn(object())


class FakeConnectCtx:
    async def __aenter__(self) -> FakeConnection:
        return FakeConnection()

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeEngine:
    """A double for the admin engine, recording that the scoped_rls glue disposed it."""

    def __init__(self) -> None:
        self.disposed = False

    def connect(self) -> FakeConnectCtx:
        return FakeConnectCtx()

    async def dispose(self) -> None:
        self.disposed = True


def test_scoped_rls_violations_reads_the_catalog_through_a_disposed_admin_engine(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The glue opens the admin engine, checks the registered scoped tables, then disposes it."""
    engine = FakeEngine()
    seen: dict[str, object] = {}

    def fake_create_engine(url: str) -> FakeEngine:
        seen["url"] = url
        return engine

    def fake_verify(sync: object, expected: set[str]) -> list[str]:
        seen["expected"] = expected
        return ["facts: missing scope_read policy"]

    monkeypatch.setattr(cli, "create_async_engine", fake_create_engine)
    monkeypatch.setattr(cli, "verify_scoped_rls", fake_verify)

    violations = asyncio.run(cli.scoped_rls_violations())

    assert violations == ["facts: missing scope_read policy"]
    assert seen["url"] == settings.admin_database_url
    assert seen["expected"] == set(cli.TableBase.metadata.info["rls"])
    assert engine.disposed


def test_capture_session_is_a_quiet_noop_without_a_transcript(
    seams: Seams, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no transcript path the Stop hook neither ingests nor enqueues, safe in any session."""
    monkeypatch.delenv(cli.TRANSCRIPT_ENV, raising=False)

    cli.app(["capture-session"], exit_on_error=False, result_action="return_value")

    assert seams.ingest.count == 0
    assert seams.enqueue.count == 0
    assert "no session transcript" in capsys.readouterr().out


@pytest.mark.parametrize("over_http", [True, False])
def test_serve_mcp_selects_the_transport_on_the_http_flag(
    seams: Seams, settings: Settings, monkeypatch: pytest.MonkeyPatch, over_http: bool
) -> None:
    """serve-mcp runs the module server over http when mcp_http is set, plain stdio otherwise."""
    monkeypatch.setattr(settings, "mcp_http", over_http)
    monkeypatch.setattr(settings, "mcp_port", 9999)

    cli.app(["serve-mcp"], exit_on_error=False, result_action="return_value")

    assert seams.serve.count == 1
    assert bool(seams.serve.kwargs) == over_http  # http carries transport kwargs, stdio runs bare
    if over_http:
        assert seams.serve.kwargs["transport"] == "http"
        assert seams.serve.kwargs["port"] == 9999
