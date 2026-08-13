from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import dbutil
import httpx
import pytest
from id_factory import uuid7
from pydantic import ValidationError

import aizk.cli as cli
import aizk.commands.client as commands
from aizk.artifacts.models import ArtifactReceipt
from aizk.client import (
    AuthenticationStatus,
    ClientProfile,
    KeepBatchResult,
    KeptFile,
    ProtocolError,
)
from aizk.mcp.models import UploadTicketAccepted
from aizk.memory import SharedDocument, ShareResult, WriteResult
from aizk.status import (
    CallerStatus,
    OrganizationStatus,
    ProcessingStatus,
    StageEstimate,
    StatusReport,
    UsageStatus,
    UsageSummary,
)
from aizk.store import Artifact


class Profiles:
    """In-memory nonsecret profile store."""

    def __init__(self) -> None:
        self.current = ClientProfile(
            server="https://stored.example/mcp", client_id="public-client"
        )
        self.saved: ClientProfile | None = None

    def load(self) -> ClientProfile:
        return self.current

    def save(self, profile: ClientProfile) -> Path:
        self.saved = profile
        return Path("profile.json")


def report(
    *,
    label: str | None = "Pedro",
    username: str | None = "pedro",
    stages: tuple[StageEstimate, ...] = (),
) -> StatusReport:
    """Build one complete status response for terminal rendering."""
    now = datetime(2026, 7, 20, tzinfo=UTC)
    return StatusReport(
        generated_at=now,
        caller=CallerStatus(
            label=label,
            username=username,
            organizations=(OrganizationStatus(name="Research"),),
        ),
        usage=UsageStatus(
            generated_at=now,
            recorded_through=now,
            days=30,
            start=now,
            summary=UsageSummary(
                requests=7,
                recalls=2,
                remembers=3,
                files=1,
                shares=1,
            ),
            lifetime=UsageSummary(requests=12, items=8),
        ),
        processing=ProcessingStatus(
            generated_at=now,
            state="active",
            stages=stages,
        ),
    )


def command_names(app: commands.App) -> set[str]:
    return set(app.resolved_commands()) - set(app.help_flags) - set(app.version_flags)


def test_root_tree_has_only_client_and_admin_surfaces() -> None:
    assert command_names(cli.app) == {
        "admin",
        "auth",
        "find",
        "keep",
        "share",
        "status",
    }
    assert command_names(commands.auth_app) == {"login", "logout", "status"}


def test_main_runs_the_command_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    command_tree = Mock()
    monkeypatch.setattr(cli, "app", command_tree)

    cli.main()

    command_tree.assert_called_once_with()


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("run `aizk auth login`"),
        PermissionError("login required"),
        ProtocolError("invalid response"),
        ValidationError.from_exception_data("status", []),
        ValueError("invalid input"),
        httpx.ConnectError("server unavailable"),
    ],
)
def test_main_renders_expected_errors_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    monkeypatch.setattr(cli, "app", Mock(side_effect=error))

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == 2
    assert capsys.readouterr().err == f"error: {error}\n"


def test_profile_resolves_explicit_and_stored_servers() -> None:
    profiles = Profiles()
    subject = commands.ClientCommands(cast("commands.ProfileStore", profiles))

    assert str(subject.profile().server) == "https://stored.example/mcp"
    assert str(subject.profile("https://explicit.example/mcp").server) == (
        "https://explicit.example/mcp"
    )


def test_login_persists_only_after_remote_authentication(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profiles = Profiles()
    login = AsyncMock(return_value=report())
    monkeypatch.setattr(
        commands,
        "MemoryClient",
        Mock(return_value=SimpleNamespace(login=login)),
    )
    subject = commands.ClientCommands(cast("commands.ProfileStore", profiles))

    dbutil.run(subject.login("https://new.example/mcp", "none", "", "localhost", 9000, 14, False))

    assert login.await_args is not None
    assert login.await_args.args == (14,)
    assert profiles.saved is not None
    assert str(profiles.saved.server) == "https://new.example/mcp"
    assert profiles.saved.callback_port == 9000
    assert "signed in as Pedro" in capsys.readouterr().out


def test_login_reuses_stored_server_and_can_render_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profiles = Profiles()
    monkeypatch.setattr(
        commands,
        "MemoryClient",
        Mock(return_value=SimpleNamespace(login=AsyncMock(return_value=report()))),
    )

    dbutil.run(
        commands.ClientCommands(cast("commands.ProfileStore", profiles)).login(
            None,
            "oauth",
            "public-client",
            "127.0.0.1",
            8912,
            30,
            True,
        )
    )

    assert '"caller"' in capsys.readouterr().out
    assert profiles.saved is not None
    assert str(profiles.saved.server) == "https://stored.example/mcp"


@pytest.mark.parametrize("json_output", [False, True])
def test_logout_clears_credentials_and_renders_both_modes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    json_output: bool,
) -> None:
    logout = AsyncMock()
    monkeypatch.setattr(
        commands,
        "MemoryClient",
        Mock(return_value=SimpleNamespace(logout=logout)),
    )

    dbutil.run(
        commands.ClientCommands(cast("commands.ProfileStore", Profiles())).logout(
            None,
            json_output,
        )
    )

    logout.assert_awaited_once_with()
    output = capsys.readouterr().out
    assert ('"server"' in output) is json_output
    assert ("signed out" in output) is not json_output


@pytest.mark.parametrize(
    ("result", "json_output", "expected"),
    [
        (
            AuthenticationStatus(
                server="https://stored.example/mcp",
                authenticated=True,
                status=report(),
            ),
            False,
            "authenticated as Pedro",
        ),
        (
            AuthenticationStatus(
                server="https://stored.example/mcp",
                authenticated=False,
            ),
            False,
            "not authenticated",
        ),
        (
            AuthenticationStatus(
                server="https://stored.example/mcp",
                authenticated=True,
                status=report(),
            ),
            True,
            '"authenticated": true',
        ),
    ],
)
def test_authentication_status_is_noninteractive_and_clear(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    result: AuthenticationStatus,
    json_output: bool,
    expected: str,
) -> None:
    status = AsyncMock(return_value=result)
    monkeypatch.setattr(
        commands,
        "MemoryClient",
        Mock(return_value=SimpleNamespace(authentication_status=status)),
    )

    dbutil.run(
        commands.ClientCommands(cast("commands.ProfileStore", Profiles())).authentication_status(
            None,
            14,
            json_output,
        )
    )

    status.assert_awaited_once_with(14)
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize("json_output", [False, True])
def test_find_forwards_query_budget_web_mode_and_output_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    json_output: bool,
) -> None:
    found = AsyncMock(return_value="Evidence")
    monkeypatch.setattr(
        commands,
        "MemoryClient",
        Mock(return_value=SimpleNamespace(find=found)),
    )

    dbutil.run(
        commands.ClientCommands(cast("commands.ProfileStore", Profiles())).find(
            "Question",
            512,
            ("Research",),
            "force",
            True,
            None,
            json_output,
        )
    )

    found.assert_awaited_once_with("Question", 512, ["Research"], "force", True)
    expected = '"Evidence"' if json_output else "Evidence"
    assert expected in capsys.readouterr().out


def test_find_rejects_missing_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands.CommandInput, "text", Mock(return_value=None))

    with pytest.raises(ValueError, match="requires a query"):
        dbutil.run(
            commands.ClientCommands(cast("commands.ProfileStore", Profiles())).find(
                None,
                None,
                (),
                "auto",
                False,
                None,
                False,
            )
        )


@pytest.mark.parametrize(
    ("source_uri", "observed_at", "expires_at", "preserve_source", "message"),
    [
        ("https://example.com", None, None, False, "source or time"),
        (None, datetime(2026, 1, 1, tzinfo=UTC), None, False, "source or time"),
        (None, None, datetime(2026, 1, 1, tzinfo=UTC), False, "source or time"),
        (None, None, None, True, "preserve-source"),
    ],
)
def test_file_paths_reject_source_only_options(
    monkeypatch: pytest.MonkeyPatch,
    source_uri: str | None,
    observed_at: datetime | None,
    expires_at: datetime | None,
    preserve_source: bool,
    message: str,
) -> None:
    monkeypatch.setattr(commands.CommandInput, "text", Mock(return_value=None))
    subject = commands.ClientCommands(cast("commands.ProfileStore", Profiles()))

    with pytest.raises(ValueError, match=message):
        dbutil.run(
            subject.keep(
                (Path("file.pdf"),),
                None,
                source_uri,
                observed_at,
                expires_at,
                (),
                preserve_source,
                None,
                False,
            )
        )


@pytest.mark.parametrize("json_output", [False, True])
def test_keep_files_accepts_paths_directly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    json_output: bool,
) -> None:
    first, second = Path("first.pdf"), Path("second.md")
    receipt = ArtifactReceipt(
        artifact_id=uuid7(),
        content_id=uuid7(),
        state=Artifact.Content.State.queued,
    )
    result = KeepBatchResult(
        files=(
            KeptFile(path=first, receipt=receipt),
            KeptFile(path=second, receipt=receipt),
        )
    )
    keep_files = AsyncMock(return_value=result)
    monkeypatch.setattr(commands.CommandInput, "text", Mock(return_value="Companion"))
    monkeypatch.setattr(
        commands,
        "MemoryClient",
        Mock(return_value=SimpleNamespace(keep_files=keep_files)),
    )

    dbutil.run(
        commands.ClientCommands(cast("commands.ProfileStore", Profiles())).keep(
            (first, second),
            "Companion",
            None,
            None,
            None,
            ("Research",),
            False,
            None,
            json_output,
        )
    )

    call = keep_files.await_args
    assert call is not None
    assert [upload.path for upload in call.args[0]] == [first, second]
    assert call.kwargs == {
        "companion_text": "Companion",
        "scopes": ["Research"],
    }
    output = capsys.readouterr().out
    assert ('"files"' in output) is json_output
    assert ("accepted 2 files" in output) is not json_output


@pytest.mark.parametrize("json_output", [False, True])
def test_keep_text_maps_to_the_mcp_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    json_output: bool,
) -> None:
    kept = WriteResult(id=uuid7())
    keep = AsyncMock(return_value=kept)
    monkeypatch.setattr(commands.CommandInput, "text", Mock(return_value="A durable fact"))
    monkeypatch.setattr(
        commands,
        "MemoryClient",
        Mock(return_value=SimpleNamespace(keep=keep)),
    )

    dbutil.run(
        commands.ClientCommands(cast("commands.ProfileStore", Profiles())).keep(
            (),
            "A durable fact",
            "https://example.com/source",
            None,
            None,
            (),
            False,
            None,
            json_output,
        )
    )

    assert keep.await_args is not None
    request = keep.await_args.args[0]
    assert request.text == "A durable fact"
    assert request.source_uri == "https://example.com/source"
    output = capsys.readouterr().out
    assert ('"id"' in output) is json_output
    assert ("kept document" in output) is not json_output


@pytest.mark.parametrize("json_output", [False, True])
def test_share_forwards_documents_and_scopes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    json_output: bool,
) -> None:
    documents = (uuid7(), uuid7())
    destination = uuid7()
    share = AsyncMock(
        return_value=ShareResult(
            documents=(
                SharedDocument(id=documents[0], title="Interpretability", destination=destination),
                SharedDocument(id=documents[1]),
            ),
            moved=True,
        )
    )
    monkeypatch.setattr(
        commands,
        "MemoryClient",
        Mock(return_value=SimpleNamespace(share=share)),
    )

    dbutil.run(
        commands.ClientCommands(cast("commands.ProfileStore", Profiles())).share(
            documents,
            None,
            ("Research",),
            True,
            20,
            False,
            None,
            json_output,
        )
    )

    assert share.await_args is not None
    request = share.await_args.args[0]
    assert tuple(request.documents) == documents
    assert request.scopes == ["Research"]
    assert request.move is True
    output = capsys.readouterr().out
    assert ('"moved"' in output) is json_output
    assert ("moved 2 documents" in output) is not json_output
    assert (f"{documents[0]}  Interpretability  now {destination}" in output) is not json_output
    assert (f"{documents[1]}  untitled" in output) is not json_output


@pytest.mark.parametrize("json_output", [False, True])
def test_expanded_status_includes_usage_and_processing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    json_output: bool,
) -> None:
    stage = StageEstimate(
        key="graph_projection",
        queued=12,
        running=None,
        failed=None,
        throughput_per_hour=8.5,
        lower_seconds=3600,
        upper_seconds=5400,
        confidence="medium",
        eta_status="estimating",
    )
    status = AsyncMock(return_value=report(stages=(stage,)))
    monkeypatch.setattr(
        commands,
        "MemoryClient",
        Mock(return_value=SimpleNamespace(status=status)),
    )

    dbutil.run(
        commands.ClientCommands(cast("commands.ProfileStore", Profiles())).status(
            30,
            None,
            json_output,
        )
    )

    status.assert_awaited_once_with(30)
    output = capsys.readouterr().out
    assert ('"processing"' in output) is json_output
    assert ("Usage over 30 days" in output) is not json_output
    assert ("ETA 1 hr to 1 hr 30 min" in output) is not json_output


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        (StageEstimate(key="conversion", eta_status="complete"), "complete"),
        (
            StageEstimate(key="conversion", failed=1, eta_status="blocked"),
            "blocked",
        ),
        (
            StageEstimate(key="conversion", eta_status="insufficient_history"),
            "ETA needs more history",
        ),
    ],
)
def test_eta_reasons_are_explicit(stage: StageEstimate, expected: str) -> None:
    assert commands.ClientCommands.render_eta(stage) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (1, "1 min"),
        (3600, "1 hr"),
        (3660, "1 hr 1 min"),
    ],
)
def test_duration_avoids_false_precision(seconds: int, expected: str) -> None:
    assert commands.ClientCommands.duration(seconds) == expected


def test_remember_renderers_cover_every_protocol_result() -> None:
    receipt = ArtifactReceipt(
        artifact_id=uuid7(),
        content_id=uuid7(),
        state=Artifact.Content.State.queued,
    )
    ticket = UploadTicketAccepted(
        upload_url="https://example.com/upload",
        expires_seconds=60,
    )

    assert "accepted file" in commands.ClientCommands.render_keep(receipt)
    assert commands.ClientCommands.render_keep(ticket) == "accepted upload ticket"


def test_command_adapters_delegate_without_business_logic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = SimpleNamespace(
        login=AsyncMock(),
        logout=AsyncMock(),
        authentication_status=AsyncMock(),
        find=AsyncMock(),
        keep=AsyncMock(),
        share=AsyncMock(),
        status=AsyncMock(),
    )
    monkeypatch.setattr(commands, "ClientCommands", Mock(return_value=instance))
    document = uuid7()

    dbutil.run(commands.login("https://example.com/mcp"))
    dbutil.run(commands.logout())
    dbutil.run(commands.authentication_status())
    dbutil.run(commands.find("question"))
    dbutil.run(commands.keep(Path("file.pdf")))
    dbutil.run(commands.share(document))
    dbutil.run(commands.status())

    instance.login.assert_awaited_once()
    instance.logout.assert_awaited_once()
    instance.authentication_status.assert_awaited_once()
    instance.find.assert_awaited_once()
    instance.keep.assert_awaited_once()
    instance.share.assert_awaited_once()
    instance.status.assert_awaited_once()
