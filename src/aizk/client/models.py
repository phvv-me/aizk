import hashlib
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from patos import FrozenModel
from pydantic import UUID7, AnyHttpUrl, Field, PositiveInt, model_validator

from ..artifacts.models import ArtifactReceipt
from ..mcp.models import UploadDeclaration
from ..status import StatusReport
from ..types import ScopeNames


class ClientProfile(FrozenModel):
    """Connection and OAuth callback settings for one remote AIZK server."""

    server: AnyHttpUrl
    auth: Literal["oauth", "none"] = "oauth"
    callback_host: str = "127.0.0.1"
    callback_port: PositiveInt = 8912
    scopes: tuple[str, ...] = ("control", "offline_access", "openid")


class LocalUpload(FrozenModel):
    """One local file and its optional wire identity overrides."""

    path: Path
    media_type: str | None = None
    filename: str | None = None

    def declaration(self) -> UploadDeclaration:
        """Hash the current file and build the exact MCP upload declaration."""
        digest = hashlib.sha256()
        size = 0
        with self.path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return UploadDeclaration(
            filename=self.filename or self.path.name,
            media_type=self.media_type
            or mimetypes.guess_type(self.path.name)[0]
            or "application/octet-stream",
            size=size,
            sha256=digest.hexdigest(),
        )


class RememberRequest(FrozenModel):
    """Typed CLI input that maps exactly onto one MCP `remember` call."""

    text: str | None = None
    source_uri: str | None = None
    observed_at: datetime | None = None
    expires_at: datetime | None = None
    scopes: ScopeNames | None = None
    preserve_source: bool = False
    upload: LocalUpload | None = None

    @model_validator(mode="after")
    def valid_mode(self) -> Self:
        """Reject combinations the MCP tool cannot execute before doing file work."""
        if self.text is None and self.source_uri is None and self.upload is None:
            raise ValueError("remember requires text, a source URI, or an upload")
        if self.upload is not None and (
            self.source_uri is not None
            or self.preserve_source
            or self.observed_at is not None
            or self.expires_at is not None
        ):
            raise ValueError(
                "upload cannot be combined with source_uri, preserve_source, "
                "observed_at, or expires_at"
            )
        return self

    def tool_arguments(
        self, declaration: UploadDeclaration | None = None
    ) -> dict[str, str | bool | list[str] | dict[str, str | int]]:
        """Serialize the request into the stable public MCP argument names."""
        arguments = self.model_dump(
            mode="json",
            exclude={"upload"},
            exclude_none=True,
            exclude_defaults=True,
        )
        if declaration is not None:
            arguments["upload"] = declaration.model_dump(mode="json")
        return arguments


class ShareRequest(FrozenModel):
    """Typed input for one provenance-preserving MCP share."""

    documents: list[UUID7] = Field(min_length=1)
    scopes: ScopeNames | None = None

    def tool_arguments(self) -> dict[str, list[str]]:
        """Serialize UUIDs and optional scopes into the MCP wire shape."""
        return self.model_dump(mode="json", exclude_none=True)


class AuthenticationStatus(FrozenModel):
    """Noninteractive credential state for one configured server."""

    server: str
    authenticated: bool
    status: StatusReport | None = None


class RememberedFile(FrozenModel):
    """One local path and the durable artifact receipt returned after upload."""

    path: Path
    receipt: ArtifactReceipt


class RememberBatchResult(FrozenModel):
    """Ordered receipts for one explicit batch of local files."""

    files: tuple[RememberedFile, ...]
