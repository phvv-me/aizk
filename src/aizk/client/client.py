from collections.abc import AsyncIterator
from pathlib import Path
from warnings import catch_warnings, filterwarnings

import anyio
import httpx
from fastmcp import Client
from fastmcp.client.auth import OAuth
from fastmcp.client.transports import StreamableHttpTransport
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.keyring import KeyringStore
from pydantic import TypeAdapter

from ..artifacts.models import ArtifactReceipt
from ..mcp.models import RememberResult, UploadTicketAccepted
from ..memory import ShareResult
from ..status import StatusReport
from .models import (
    AuthenticationStatus,
    ClientProfile,
    LocalUpload,
    RememberBatchResult,
    RememberedFile,
    RememberRequest,
    ShareRequest,
)

_REMEMBER_RESULT = TypeAdapter[RememberResult](RememberResult)


class LoginRequiredError(PermissionError):
    """The selected server has no usable stored OAuth session."""


class ProtocolError(RuntimeError):
    """The remote MCP and upload responses violate the AIZK contract."""


class NonInteractiveOAuth(OAuth):
    """Refresh stored OAuth state while refusing to start browser authorization."""

    async def redirect_handler(self, authorization_url: str) -> None:
        """Stop when authorization would require user interaction."""
        raise LoginRequiredError("AIZK login is required")


class MemoryClient:
    """Call the public AIZK MCP tools with persistent least-surprise authentication."""

    def __init__(
        self,
        profile: ClientProfile,
        *,
        token_storage: AsyncKeyValue | None = None,
        upload_http: httpx.AsyncClient | None = None,
    ) -> None:
        self.profile = profile
        self.token_storage: AsyncKeyValue | None
        if token_storage is not None:
            self.token_storage = token_storage
        elif profile.auth == "oauth":
            with catch_warnings():
                filterwarnings(
                    "ignore",
                    message="A configured store is unstable and may change.*",
                    category=UserWarning,
                )
                self.token_storage = KeyringStore(service_name="aizk.oauth")
        else:
            self.token_storage = None
        self.upload_http = upload_http

    def oauth(self, interactive: bool) -> OAuth:
        """Build one OAuth provider over the shared persistent credential store."""
        if self.token_storage is None:
            raise RuntimeError("OAuth token storage is unavailable")
        provider = OAuth if interactive else NonInteractiveOAuth
        return provider(
            mcp_url=str(self.profile.server),
            scopes=list(self.profile.scopes),
            client_name="AIZK CLI",
            token_storage=self.token_storage,
            callback_host=self.profile.callback_host,
            callback_port=self.profile.callback_port,
        )

    def connection(self, interactive: bool = False) -> Client[StreamableHttpTransport]:
        """Build one short MCP connection without silently opening a browser."""
        auth = self.oauth(interactive) if self.profile.auth == "oauth" else None
        return Client(StreamableHttpTransport(str(self.profile.server), auth=auth))

    async def login(self, days: int = 30) -> StatusReport:
        """Run interactive OAuth and prove the resulting session through `status`."""
        return await self.status(days=days, interactive=True)

    async def authentication_status(self, days: int = 30) -> AuthenticationStatus:
        """Validate or refresh stored credentials without starting login."""
        try:
            status = await self.status(days=days)
        except LoginRequiredError:
            return AuthenticationStatus(
                server=str(self.profile.server),
                authenticated=False,
            )
        return AuthenticationStatus(
            server=str(self.profile.server),
            authenticated=True,
            status=status,
        )

    async def logout(self) -> None:
        """Forget tokens, expiry, and dynamic registration for this server only."""
        if self.profile.auth == "oauth":
            await self.oauth(interactive=False).token_storage_adapter.clear()

    async def require_credentials(self) -> None:
        """Fail cleanly before FastMCP attempts an interactive OAuth flow."""
        if self.profile.auth == "oauth":
            tokens = await self.oauth(interactive=False).token_storage_adapter.get_tokens()
            if tokens is None:
                raise LoginRequiredError("run `aizk auth login` first")

    async def status(self, days: int = 30, *, interactive: bool = False) -> StatusReport:
        """Return caller identity, durable usage, and processing health."""
        if not interactive:
            await self.require_credentials()
        async with self.connection(interactive) as client:
            result = await client.call_tool("status", {"days": days})
        return TypeAdapter(StatusReport).validate_python(result.data)

    async def recall(self, query: str, budget: int | None = None) -> str:
        """Return visible evidence for one natural-language question."""
        arguments: dict[str, str | int] = {"query": query}
        if budget is not None:
            arguments["budget"] = budget
        await self.require_credentials()
        async with self.connection() as client:
            result = await client.call_tool("recall", arguments)
        return TypeAdapter(str).validate_python(result.data)

    async def remember(self, request: RememberRequest) -> RememberResult:
        """Remember text, a URI, or one local file through the two-step upload flow."""
        declaration = request.upload.declaration() if request.upload is not None else None
        await self.require_credentials()
        async with self.connection() as client:
            result = await client.call_tool(
                "remember",
                request.tool_arguments(declaration),
            )
        remembered = _REMEMBER_RESULT.validate_python(result.data)
        if request.upload is None:
            if isinstance(remembered, UploadTicketAccepted):
                raise ProtocolError("server returned an upload ticket without an upload")
            return remembered
        if not isinstance(remembered, UploadTicketAccepted):
            raise ProtocolError("server did not return a ticket for the declared upload")
        return await self.upload(remembered, request.upload.path)

    async def share(self, request: ShareRequest) -> ShareResult:
        """Copy visible documents into one authorized destination."""
        await self.require_credentials()
        async with self.connection() as client:
            result = await client.call_tool("share", request.tool_arguments())
        return TypeAdapter(ShareResult).validate_python(result.data)

    async def remember_files(
        self,
        uploads: list[LocalUpload],
        *,
        companion_text: str | None = None,
        scopes: list[str] | None = None,
    ) -> RememberBatchResult:
        """Remember explicit local files in order and redeem every ticket internally."""
        if not uploads:
            raise ValueError("remember_files requires at least one file")
        remembered: list[RememberedFile] = []
        for upload in uploads:
            result = await self.remember(
                RememberRequest(
                    text=companion_text,
                    scopes=scopes,
                    upload=upload,
                )
            )
            if not isinstance(result, ArtifactReceipt):
                raise ProtocolError("file upload did not return an artifact receipt")
            remembered.append(RememberedFile(path=upload.path, receipt=result))
        return RememberBatchResult(files=tuple(remembered))

    async def upload(
        self,
        ticket: UploadTicketAccepted,
        path: Path,
    ) -> ArtifactReceipt:
        """PUT the declared bytes once without forwarding OAuth credentials."""
        content = self.content(path)
        if self.upload_http is not None:
            response = await self.upload_http.put(ticket.upload_url, content=content)
        else:
            async with httpx.AsyncClient(follow_redirects=False) as client:
                response = await client.put(ticket.upload_url, content=content)
        response.raise_for_status()
        return ArtifactReceipt.model_validate_json(response.content)

    @staticmethod
    async def content(path: Path) -> AsyncIterator[bytes]:
        """Stream one local file in bounded chunks."""
        async with await anyio.open_file(path, "rb") as stream:
            while chunk := await stream.read(1024 * 1024):
                yield chunk
