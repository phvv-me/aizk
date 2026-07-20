import hashlib
from datetime import UTC, datetime
from typing import cast
from uuid import NAMESPACE_URL, uuid5

import dbutil
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from pydantic import ValidationError

from aizk.artifacts.uploads import (
    UploadGrant,
    UploadGrantLimitError,
    UploadRequest,
)
from aizk.config import settings
from aizk.mcp import server as mcp_server
from aizk.mcp.server import AizkMCP, UploadTicketAccepted
from aizk.store.identity import User


class _MintingUploads:
    def __init__(self) -> None:
        self.declared: UploadRequest | None = None

    async def mint(self, user: User, declared: UploadRequest) -> UploadGrant:
        self.declared = declared
        return UploadGrant(
            url="https://aizk.example/api/uploads/opaque-capability",
            expires_seconds=600,
        )


class _FailingUploads:
    def __init__(self, error: ValueError | UploadGrantLimitError) -> None:
        self.error = error

    async def mint(self, user: User, declared: UploadRequest) -> UploadGrant:
        raise self.error


class _Server:
    def __init__(self, uploads: object) -> None:
        self.settings = settings
        self.uploads = uploads
        self.identity = User.model_construct(
            id=uuid5(NAMESPACE_URL, "https://aizk.example/mcp-user")
        )

    async def user(self, context: Context, identified: bool = False) -> User:
        assert identified
        return self.identity


def upload() -> mcp_server.UploadDeclaration:
    content = b"declared upload"
    return mcp_server.UploadDeclaration(
        filename="evidence.txt",
        media_type="text/plain",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def test_remember_upload_mints_minimal_hash_bound_ticket() -> None:
    uploads = _MintingUploads()
    server = cast(AizkMCP, _Server(uploads))
    remember = AizkMCP.remember_tool(server)

    result = dbutil.run(
        remember(
            context=cast(Context, object()),
            text="Companion context.",
            scopes=["Research"],
            upload=upload(),
        )
    )

    assert isinstance(result, UploadTicketAccepted)
    assert set(result.model_dump()) == {"status", "upload_url", "expires_seconds"}
    assert result.status == "accepted"
    assert result.upload_url == "https://aizk.example/api/uploads/opaque-capability"
    assert result.expires_seconds == 600
    assert uploads.declared is not None
    assert uploads.declared.sha256 == upload().sha256
    assert uploads.declared.companion_text == "Companion context."
    assert uploads.declared.scopes == ["Research"]


@pytest.mark.parametrize(
    ("source_uri", "preserve_source", "observed_at", "expires_at"),
    (
        ("https://example.org/source", False, None, None),
        (None, True, None, None),
        (None, False, datetime(2026, 1, 1, tzinfo=UTC), None),
        (None, False, None, datetime(2026, 1, 1, tzinfo=UTC)),
    ),
)
def test_remember_upload_rejects_uri_and_temporal_modes(
    source_uri: str | None,
    preserve_source: bool,
    observed_at: datetime | None,
    expires_at: datetime | None,
) -> None:
    uploads = _MintingUploads()
    server = cast(AizkMCP, _Server(uploads))
    remember = AizkMCP.remember_tool(server)

    with pytest.raises(ToolError, match="file upload cannot be combined"):
        dbutil.run(
            remember(
                context=cast(Context, object()),
                source_uri=source_uri,
                preserve_source=preserve_source,
                observed_at=observed_at,
                expires_at=expires_at,
                upload=upload(),
            )
        )

    assert uploads.declared is None


def test_upload_declaration_advertises_and_validates_sha256() -> None:
    schema = mcp_server.UploadDeclaration.model_json_schema()

    assert "sha256" in schema["required"]
    assert schema["properties"]["sha256"]["pattern"] == "^[0-9a-f]{64}$"
    with pytest.raises(ValidationError):
        mcp_server.UploadDeclaration(
            filename="evidence.txt",
            media_type="text/plain",
            size=8,
            sha256="not-a-sha256",
        )


@pytest.mark.parametrize(
    ("error", "message"),
    (
        (ValueError("invalid upload"), "invalid upload"),
        (UploadGrantLimitError("too many live upload grants"), "too many live upload grants"),
    ),
)
def test_remember_upload_translates_mint_failures(
    error: ValueError | UploadGrantLimitError,
    message: str,
) -> None:
    server = cast(AizkMCP, _Server(_FailingUploads(error)))
    remember = AizkMCP.remember_tool(server)

    with pytest.raises(ToolError, match=message):
        dbutil.run(remember(context=cast(Context, object()), upload=upload()))
