import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import TracebackType
from typing import Literal, cast

import dbutil
import httpx
import pytest
from fastmcp.client.client import CallToolResult
from id_factory import uuid7
from key_value.aio.stores.keyring import KeyringStore
from key_value.aio.stores.memory import MemoryStore
from pydantic import JsonValue

import aizk.client.client as client_module
from aizk.artifacts.models import ArtifactReceipt
from aizk.client import (
    ClientProfile,
    CommandInput,
    LocalUpload,
    LoginRequiredError,
    MemoryClient,
    ProfileStore,
    RememberRequest,
    ResultSerializer,
    ShareRequest,
)
from aizk.client.client import NonInteractiveOAuth, ProtocolError
from aizk.mcp.models import UploadTicketAccepted
from aizk.memory import WriteResult
from aizk.status import (
    CallerStatus,
    ProcessingStatus,
    StatusReport,
    UsageStatus,
    UsageSummary,
)
from aizk.store import Artifact


class ToolClient:
    """Minimal FastMCP client double returning one typed wire payload."""

    def __init__(self, data: JsonValue) -> None:
        self.data = data
        self.calls: list[tuple[str, dict[str, JsonValue]]] = []

    async def __aenter__(self) -> ToolClient:
        return self

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        return CallToolResult(
            content=[],
            structured_content={},
            meta=None,
            data=self.data,
        )


class InputStream(StringIO):
    """String input with an explicit terminal standing."""

    def __init__(self, value: str, terminal: bool) -> None:
        super().__init__(value)
        self.terminal = terminal

    def isatty(self) -> bool:
        """Return the configured stream standing."""
        return self.terminal


def profile(auth: Literal["oauth", "none"] = "none") -> ClientProfile:
    """Build one deterministic client profile."""
    return ClientProfile.model_validate(
        {
            "server": "https://aizk.example/mcp",
            "auth": auth,
        }
    )


def status_report() -> StatusReport:
    """Build one small complete status response."""
    now = datetime(2026, 7, 20, tzinfo=UTC)
    return StatusReport(
        generated_at=now,
        caller=CallerStatus(name="Pedro", label="Pedro"),
        usage=UsageStatus(
            generated_at=now,
            recorded_through=now,
            days=30,
            start=now,
            summary=UsageSummary(requests=2),
            lifetime=UsageSummary(requests=5),
        ),
        processing=ProcessingStatus(
            generated_at=now,
            state="idle",
            stages=(),
        ),
    )


def test_oauth_defaults_to_the_system_keyring_without_a_file_fallback() -> None:
    client = MemoryClient(profile("oauth"))

    assert isinstance(client.token_storage, KeyringStore)


def test_oauth_connections_are_explicitly_interactive_or_noninteractive() -> None:
    storage = MemoryStore()
    client = MemoryClient(profile("oauth"), token_storage=storage)

    assert isinstance(client.oauth(interactive=False), NonInteractiveOAuth)
    assert type(client.oauth(interactive=True)) is not NonInteractiveOAuth
    assert isinstance(client.connection().transport.auth, NonInteractiveOAuth)
    assert MemoryClient(profile()).connection().transport.auth is None
    with pytest.raises(RuntimeError, match="storage is unavailable"):
        MemoryClient(profile()).oauth(interactive=False)
    with pytest.raises(LoginRequiredError):
        dbutil.run(
            NonInteractiveOAuth(
                mcp_url="https://aizk.example/mcp",
                token_storage=storage,
            ).redirect_handler("https://login.example")
        )


def test_profile_store_round_trips_only_nonsecret_connection_preferences(
    tmp_path: Path,
) -> None:
    path = tmp_path / "aizk" / "profile.json"
    store = ProfileStore(path)
    expected = ClientProfile.model_validate(
        {
            "server": "https://memory.example/mcp",
            "auth": "oauth",
            "callback_host": "localhost",
            "callback_port": 9123,
        }
    )

    saved = store.save(expected)

    assert saved == path
    assert store.load() == expected
    serialized = path.read_text(encoding="utf-8")
    assert tuple(json.loads(serialized)) == (
        "auth",
        "callback_host",
        "callback_port",
        "scopes",
        "server",
    )
    assert "token" not in serialized
    assert "secret" not in serialized


def test_profile_store_points_an_unconfigured_client_to_login(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"

    with pytest.raises(FileNotFoundError, match="aizk auth login --server"):
        ProfileStore(path).load()


def test_profile_store_uses_the_xdg_config_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert ProfileStore.default_path() == tmp_path / "aizk" / "profile.json"


def test_profile_store_falls_back_to_the_user_config_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert ProfileStore.default_path() == tmp_path / ".config" / "aizk" / "profile.json"


def test_noninteractive_authentication_status_never_starts_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def login_required(
        self: MemoryClient,
        days: int = 30,
        *,
        interactive: bool = False,
    ) -> StatusReport:
        assert not interactive
        raise LoginRequiredError("login required")

    monkeypatch.setattr(MemoryClient, "status", login_required)

    result = dbutil.run(MemoryClient(profile()).authentication_status())

    assert result.authenticated is False
    assert result.status is None


def test_login_and_authenticated_status_return_the_typed_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = status_report()
    interactions: list[bool] = []

    async def status(
        self: MemoryClient,
        days: int = 30,
        *,
        interactive: bool = False,
    ) -> StatusReport:
        assert days == 14
        interactions.append(interactive)
        return expected

    monkeypatch.setattr(MemoryClient, "status", status)
    client = MemoryClient(profile())

    logged_in = dbutil.run(client.login(days=14))
    authenticated = dbutil.run(client.authentication_status(days=14))

    assert logged_in is expected
    assert authenticated.authenticated is True
    assert authenticated.status is expected
    assert interactions == [True, False]


def test_logout_clears_only_the_selected_server_oauth_keys() -> None:
    storage = MemoryStore()
    client = MemoryClient(profile("oauth"), token_storage=storage)
    server = str(client.profile.server).rstrip("/")
    other = "https://other.example/mcp"

    async def exercise() -> None:
        for url in (server, other):
            await storage.put(
                key=f"{url}/tokens",
                value={"access_token": "secret"},
                collection="mcp-oauth-token",
            )
            await storage.put(
                key=f"{url}/client_info",
                value={"client_id": "registered"},
                collection="mcp-oauth-client-info",
            )
            await storage.put(
                key=f"{url}/token_expiry",
                value={"expires_at": 1},
                collection="mcp-oauth-token-expiry",
            )
        await client.logout()

    dbutil.run(exercise())

    async def remaining() -> tuple[dict[str, str] | None, dict[str, str] | None]:
        selected = await storage.get(
            key=f"{server}/tokens",
            collection="mcp-oauth-token",
        )
        untouched = await storage.get(
            key=f"{other}/tokens",
            collection="mcp-oauth-token",
        )
        return cast("dict[str, str] | None", selected), cast("dict[str, str] | None", untouched)

    selected, untouched = dbutil.run(remaining())
    assert selected is None
    assert untouched == {"access_token": "secret"}
    dbutil.run(MemoryClient(profile()).logout())


def test_recall_and_share_keep_the_mcp_wire_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recall = ToolClient("evidence")
    shared = ToolClient({"shared": 2})
    clients = iter((recall, shared))
    monkeypatch.setattr(MemoryClient, "connection", lambda self, interactive=False: next(clients))
    client = MemoryClient(profile())
    first, second = uuid7(), uuid7()

    async def exercise() -> tuple[str, int]:
        evidence = await client.recall("what changed", budget=512)
        result = await client.share(
            ShareRequest(
                documents=[first, second],
                scopes=["Research"],
            )
        )
        return evidence, result.shared

    evidence, count = dbutil.run(exercise())

    assert evidence == "evidence"
    assert count == 2
    assert recall.calls == [("recall", {"query": "what changed", "budget": 512})]
    assert shared.calls == [
        (
            "share",
            {
                "documents": [
                    str(first),
                    str(second),
                ],
                "scopes": ["Research"],
            },
        )
    ]


def test_recall_omits_an_unspecified_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = ToolClient("evidence")
    monkeypatch.setattr(MemoryClient, "connection", lambda self, interactive=False: remote)

    assert dbutil.run(MemoryClient(profile()).recall("question")) == "evidence"
    assert remote.calls == [("recall", {"query": "question"})]


def test_remember_text_returns_the_typed_write_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = uuid7()
    remote = ToolClient({"id": str(document)})
    monkeypatch.setattr(MemoryClient, "connection", lambda self, interactive=False: remote)

    result = dbutil.run(MemoryClient(profile()).remember(RememberRequest(text="literal path.txt")))

    assert result == WriteResult(id=document)
    assert remote.calls == [("remember", {"text": "literal path.txt"})]


def test_remember_rejects_upload_protocol_mismatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    ticket = ToolClient(
        {
            "status": "accepted",
            "upload_url": "https://upload.example/opaque",
            "expires_seconds": 60,
        }
    )
    write = ToolClient({"id": str(uuid7())})
    clients = iter((ticket, write))
    monkeypatch.setattr(MemoryClient, "connection", lambda self, interactive=False: next(clients))
    client = MemoryClient(profile())

    with pytest.raises(ProtocolError, match="without an upload"):
        dbutil.run(client.remember(RememberRequest(text="plain")))
    with pytest.raises(ProtocolError, match="did not return a ticket"):
        dbutil.run(client.remember(RememberRequest(upload=LocalUpload(path=source))))


def test_remember_upload_hashes_streams_and_returns_the_final_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "evidence.txt"
    source.write_bytes(b"durable evidence")
    remote = ToolClient(
        {
            "status": "accepted",
            "upload_url": "https://upload.example/api/uploads/opaque",
            "expires_seconds": 60,
        }
    )
    monkeypatch.setattr(MemoryClient, "connection", lambda self, interactive=False: remote)
    receipt = ArtifactReceipt(
        artifact_id=uuid7(),
        content_id=uuid7(),
        state=Artifact.Content.State.queued,
    )

    async def receive(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") is None
        assert await request.aread() == b"durable evidence"
        return httpx.Response(200, json=receipt.model_dump(mode="json"))

    async def exercise() -> ArtifactReceipt:
        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as http:
            client = MemoryClient(profile(), upload_http=http)
            return cast(
                "ArtifactReceipt",
                await client.remember(
                    RememberRequest(
                        text="Companion",
                        scopes=["Research"],
                        upload=LocalUpload(path=source),
                    )
                ),
            )

    result = dbutil.run(exercise())

    assert result == receipt
    name, arguments = remote.calls[0]
    assert name == "remember"
    assert arguments["text"] == "Companion"
    assert arguments["scopes"] == ["Research"]
    assert arguments["upload"] == {
        "filename": "evidence.txt",
        "media_type": "text/plain",
        "sha256": "03f04bd9bde76fc0a793a58f9d09cad7a471b735a6d0f099ed312051df73cef3",
        "size": 16,
    }


def test_remember_files_batches_only_explicit_paths_and_keeps_text_literal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.pdf"
    first.write_text("one", encoding="utf-8")
    second.write_bytes(b"two")
    receipts = (
        ArtifactReceipt(
            artifact_id=uuid7(),
            content_id=uuid7(),
            state=Artifact.Content.State.queued,
        ),
        ArtifactReceipt(
            artifact_id=uuid7(),
            content_id=uuid7(),
            state=Artifact.Content.State.queued,
        ),
    )
    requests: list[RememberRequest] = []

    async def remember(
        self: MemoryClient,
        request: RememberRequest,
    ) -> ArtifactReceipt:
        requests.append(request)
        return receipts[len(requests) - 1]

    monkeypatch.setattr(MemoryClient, "remember", remember)
    client = MemoryClient(profile())
    result = dbutil.run(
        client.remember_files(
            [LocalUpload(path=first), LocalUpload(path=second)],
            companion_text=str(first),
            scopes=["Research"],
        )
    )

    assert tuple(item.path for item in result.files) == (first, second)
    assert tuple(item.receipt for item in result.files) == receipts
    assert [request.text for request in requests] == [str(first), str(first)]
    assert [request.upload.path for request in requests if request.upload is not None] == [
        first,
        second,
    ]
    assert [request.scopes for request in requests] == [["Research"], ["Research"]]


def test_remember_files_refuses_an_empty_batch() -> None:
    with pytest.raises(ValueError, match="at least one"):
        dbutil.run(MemoryClient(profile()).remember_files([]))


def test_remember_files_rejects_a_nonreceipt_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")

    async def remember(
        self: MemoryClient,
        request: RememberRequest,
    ) -> WriteResult:
        return WriteResult(id=uuid7())

    monkeypatch.setattr(MemoryClient, "remember", remember)
    with pytest.raises(ProtocolError, match="artifact receipt"):
        dbutil.run(MemoryClient(profile()).remember_files([LocalUpload(path=source)]))


def test_upload_builds_a_nonredirecting_client_when_none_was_injected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    receipt = ArtifactReceipt(
        artifact_id=uuid7(),
        content_id=uuid7(),
        state=Artifact.Content.State.queued,
    )

    async def receive(request: httpx.Request) -> httpx.Response:
        assert await request.aread() == b"source"
        return httpx.Response(200, json=receipt.model_dump(mode="json"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    monkeypatch.setattr(client_module.httpx, "AsyncClient", lambda follow_redirects=False: http)
    ticket = UploadTicketAccepted(
        upload_url="https://upload.example/opaque",
        expires_seconds=60,
    )

    assert dbutil.run(MemoryClient(profile()).upload(ticket, source)) == receipt


def test_status_parses_the_expanded_typed_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = status_report()
    remote = ToolClient(expected.model_dump(mode="json"))
    monkeypatch.setattr(MemoryClient, "connection", lambda self, interactive=False: remote)

    result = dbutil.run(MemoryClient(profile()).status(days=14))

    assert result == expected
    assert remote.calls == [("status", {"days": 14})]


def test_command_text_does_not_block_a_tty_and_json_is_constant() -> None:
    terminal = InputStream("ignored", terminal=True)
    pipe = InputStream("piped", terminal=False)

    assert CommandInput.text(None, terminal) is None
    assert CommandInput.text(None, pipe) == "piped"
    assert CommandInput.text("explicit", pipe) == "explicit"
    assert ResultSerializer.json(UsageSummary(requests=2, recalls=1)) == (
        "{\n"
        '  "artifact_reads": 0,\n'
        '  "downloaded_bytes": 0,\n'
        '  "duration_ms": 0.0,\n'
        '  "files": 0,\n'
        '  "items": 0,\n'
        '  "recalls": 1,\n'
        '  "remembers": 0,\n'
        '  "request_bytes": 0,\n'
        '  "requests": 2,\n'
        '  "response_bytes": 0,\n'
        '  "shares": 0,\n'
        '  "uploaded_bytes": 0\n'
        "}"
    )


@pytest.mark.parametrize(
    "invalid",
    (
        {},
        {"upload": LocalUpload(path=Path("source")), "source_uri": "https://example.org"},
        {"upload": LocalUpload(path=Path("source")), "preserve_source": True},
        {
            "upload": LocalUpload(path=Path("source")),
            "observed_at": datetime(2026, 7, 20, tzinfo=UTC),
        },
        {
            "upload": LocalUpload(path=Path("source")),
            "expires_at": datetime(2026, 7, 20, tzinfo=UTC),
        },
    ),
)
def test_remember_request_rejects_invalid_modes(
    invalid: dict[str, JsonValue | LocalUpload | datetime],
) -> None:
    with pytest.raises(ValueError):
        RememberRequest.model_validate(invalid)


def test_local_upload_honors_wire_overrides_and_unknown_media_fallback(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.unknown"
    source.write_bytes(b"x")

    overridden = LocalUpload(
        path=source,
        filename="renamed.data",
        media_type="application/custom",
    ).declaration()
    fallback = LocalUpload(path=source).declaration()

    assert overridden.filename == "renamed.data"
    assert overridden.media_type == "application/custom"
    assert fallback.media_type == "application/octet-stream"
