from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.resources import ResourceContent, ResourceResult
from fastmcp.server.context import Context
from obstore.exceptions import BaseError as ObjectStoreError
from patos import FrozenModel
from pydantic import UUID5, UUID7, UUID8, Field, StringConstraints

from ..artifacts.models import ArtifactReceipt
from ..artifacts.service import ArtifactIntake
from ..artifacts.uploads import Sha256Hex, UploadBox, UploadGrantLimitError, UploadRequest
from ..auth import Auth
from ..config import Settings
from ..integrations.clamav import MalwareRejectedError, MalwareUnavailableError
from ..memory import Memory, ShareResult, WriteResult
from ..storage import ByteStore, IntegrityMismatch
from ..store import Artifact, Blob, Usage
from ..store.identity import User
from ..types import ScopeNames
from ..usage import annotate_operation
from .middleware import CallerRateLimit, IdentityMiddleware, bound_user


class _ArtifactObject(FrozenModel):
    """Authorized object-store fields needed to materialize one original artifact."""

    storage_key: str
    storage_version: str | None = None
    content_hash: UUID8
    size: int
    encoding: Blob.Encoding
    scopes: list[UUID5]
    media_type: str | None = None


class _UploadDeclaration(FrozenModel):
    """One local file whose exact bytes an agent intends to preserve."""

    filename: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]
    media_type: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]
    size: Annotated[int, Field(gt=0)]
    sha256: Sha256Hex


class UploadTicketAccepted(FrozenModel):
    """Acknowledge one short-lived private upload ticket."""

    status: Literal["accepted"] = "accepted"
    capability: str
    instruction: str


type _RememberResult = WriteResult | ArtifactReceipt | UploadTicketAccepted


class AizkMCP(FastMCP):
    """Expose Aizk's authenticated memory tools through FastMCP.

    The composition root constructs one server per process with its verifier, byte
    store, upload box, and artifact services. Identity middleware resolves one
    Logto-backed `User` before each call, a per-caller token bucket limits sustained
    work, and PostgreSQL row security remains the final authorization boundary for
    every retrieved or written row. Tool input bounds are read from settings when the
    server is built, never at import time.
    """

    def __init__(
        self,
        auth: Auth,
        store: ByteStore,
        uploads: UploadBox,
        intake: ArtifactIntake,
        config: Settings,
        name: str = "aizk",
    ) -> None:
        self.authentication = auth
        self.store = store
        self.uploads = uploads
        self.intake = intake
        self.settings = config
        super().__init__(name, auth=auth.provider())
        self.add_middleware(IdentityMiddleware(auth))
        self.add_middleware(
            CallerRateLimit(max_requests_per_second=config.mcp_request_rate_per_second)
        )
        for verb in (
            self.status_tool(),
            self.recall_tool(),
            self.remember_tool(),
            self.share_tool(),
        ):
            self.tool(verb)
        self.resource(
            "aizk://artifacts/{artifact_id}/contents/{artifact_content_id}",
            name="artifact",
            description="Read one exact visible original artifact revision on demand.",
        )(self.artifact_resource())

    async def user(self, context: Context, identified: bool = False) -> User:
        """Return the request's resolved caller and optionally require authentication."""
        if (user := await bound_user(context)) is None:
            raise ToolError("no user resolved for this call")
        if identified and user.is_anonymous():
            raise ToolError("anonymous callers are read-only, authenticate to write")
        return user

    def memory(self, user: User) -> Memory:
        """Build the shared memory service bound to one resolved caller."""
        return Memory(user=user, intake=self.intake)

    def status_tool(self) -> Callable[[Context], Coroutine[None, None, User]]:
        """Build the `status` tool over this server's dependencies."""

        async def status(context: Context) -> User:
            """Return the caller's organizations, roles, and permissions as supplied by
            Logto."""
            return self.memory(await self.user(context, identified=True)).status

        return status

    def recall_tool(self) -> Callable[..., Coroutine[None, None, str]]:
        """Build the `recall` tool with input bounds from this server's settings."""
        config = self.settings

        async def recall(
            query: Annotated[
                str,
                StringConstraints(
                    strip_whitespace=True,
                    min_length=1,
                    max_length=config.mcp_recall_query_max_chars,
                ),
            ],
            context: Context,
            budget: Annotated[
                int, Field(gt=0, le=config.mcp_recall_budget_max_tokens)
            ] = config.context_token_budget,
        ) -> str:
            """Return visible evidence for one question as clear, ordered Markdown.

            query: natural-language question whose length is bounded by deployment settings.
            budget: optional evidence cap. Omit it unless repeated responses are too long.
            """
            if not (query := query.strip()):
                raise ToolError("recall query cannot be blank")
            memory = self.memory(await self.user(context))
            return await (await memory.recall(query, budget)).to_markdown()

        return recall

    def remember_tool(self) -> Callable[..., Coroutine[None, None, _RememberResult]]:
        """Build the `remember` tool with input bounds from this server's settings."""
        config = self.settings

        async def remember(
            context: Context,
            text: Annotated[
                str,
                StringConstraints(
                    strip_whitespace=True,
                    min_length=1,
                    max_length=config.mcp_remember_max_chars,
                ),
            ]
            | None = None,
            source_uri: Annotated[
                str, StringConstraints(max_length=config.mcp_source_uri_max_chars)
            ]
            | None = None,
            observed_at: datetime | None = None,
            expires_at: datetime | None = None,
            scopes: Annotated[ScopeNames, Field(max_length=config.mcp_scope_names_max)]
            | None = None,
            preserve_source: bool = False,
            upload: _UploadDeclaration | None = None,
        ) -> _RememberResult:
            """Store text, preserve one URI original, or prepare one local file upload.

            text: self-describing Markdown, plain text, or companion information for a
                preserved URI or uploaded file.
            source_uri: original website or file URL. Omission keeps text mode local.
            observed_at: optional time when the statement became applicable. Normally omitted.
            expires_at: known time after which the statement stops being true. It is not a
                reminder. Normally omitted.
            scopes: optional authorized Logto organization names. Omission means private memory.
            preserve_source: download and retain `source_uri` as an original file. Omit this
                unless the exact contract, form, presentation, paper, or other source may be
                needed later. A URI without `text` is always preserved.
            upload: exact filename, media type, byte size, and SHA-256 for one local file.
                This mode cannot be combined with URI or temporal inputs. It returns a
                short-lived one-time private upload ticket, not a stored artifact receipt.
            """
            if text is not None:
                text = text.strip() or None
            if upload is not None:
                if (
                    source_uri is not None
                    or preserve_source
                    or observed_at is not None
                    or expires_at is not None
                ):
                    raise ToolError(
                        "file upload cannot be combined with source_uri, preserve_source, "
                        "observed_at, or expires_at"
                    )
                user = await self.user(context, identified=True)
                try:
                    declared = UploadRequest(
                        filename=upload.filename,
                        media_type=upload.media_type,
                        size=upload.size,
                        sha256=upload.sha256,
                        scopes=scopes,
                        companion_text=text,
                    )
                    grant = await self.uploads.mint(user, declared)
                except ValueError as invalid:
                    raise ToolError(str(invalid)) from invalid
                except UploadGrantLimitError as saturated:
                    raise ToolError(str(saturated)) from saturated
                return UploadTicketAccepted(
                    capability=grant.url.rsplit("/", 1)[-1],
                    instruction=(
                        "PUT the exact declared bytes once to the private single-use upload "
                        f"endpoint {grant.url}. It expires shortly, in "
                        f"{grant.expires_seconds} seconds."
                    ),
                )
            if text is None and source_uri is None:
                raise ToolError("remember requires text or a source URI")
            user = await self.user(context, identified=True)
            try:
                return await self.memory(user).remember(
                    text,
                    source_uri=source_uri,
                    observed_at=observed_at,
                    expires_at=expires_at,
                    scopes=scopes,
                    preserve_source=preserve_source,
                )
            except MalwareRejectedError as rejected:
                raise ToolError("the source was rejected by the safety scan") from rejected
            except MalwareUnavailableError as unavailable:
                raise ToolError("safety scanning is temporarily unavailable") from unavailable
            except ObjectStoreError as unavailable:
                raise ToolError("object storage is temporarily unavailable") from unavailable
            except httpx.HTTPError as unavailable:
                raise ToolError("the source URI could not be fetched") from unavailable
            except ValueError as invalid:
                raise ToolError(str(invalid)) from invalid

        return remember

    def share_tool(self) -> Callable[..., Coroutine[None, None, ShareResult]]:
        """Build the `share` tool with input bounds from this server's settings."""
        config = self.settings

        async def share(
            documents: Annotated[
                list[UUID7], Field(min_length=1, max_length=config.mcp_share_documents_max)
            ],
            context: Context,
            scopes: Annotated[ScopeNames, Field(max_length=config.mcp_scope_names_max)]
            | None = None,
        ) -> ShareResult:
            """Copy visible documents into one authorized destination without moving sources.

            documents: visible document IDs to copy, bounded per call.
            scopes: optional authorized Logto organization names. Omission means private memory.
            """
            user = await self.user(context, identified=True)
            return await self.memory(user).share(documents, scopes)

        return share

    def artifact_resource(self) -> Callable[..., Coroutine[None, None, ResourceResult]]:
        """Build the artifact resource reader over this server's byte store."""

        async def read_artifact(
            artifact_id: UUID7,
            artifact_content_id: UUID7,
            context: Context,
        ) -> ResourceResult:
            """Read exact original bytes that grounded evidence visible to the current caller.

            artifact_id: artifact named by the resource URI.
            artifact_content_id: immutable original revision named by the resource URI.
            """
            user = await self.user(context)
            rows = await user.exec[_ArtifactObject](
                Artifact.Content.original(artifact_id, artifact_content_id)
            )
            if not rows:
                raise ResourceError("artifact is not visible or does not exist")
            original = rows[0]
            # Attribute the read to the scopes that own the artifact, not the caller.
            annotate_operation(Usage.Event.Operation.artifact_read, original.scopes)
            try:
                content = await self.store.get(
                    original.storage_key,
                    encoding=original.encoding,
                    expected_size=original.size,
                    expected_hash=original.content_hash,
                    version=original.storage_version,
                )
            except IntegrityMismatch as invalid:
                raise ResourceError("artifact bytes failed integrity verification") from invalid
            except ObjectStoreError as unavailable:
                raise ResourceError("object storage is temporarily unavailable") from unavailable
            return ResourceResult(
                contents=[ResourceContent(content, mime_type=original.media_type)]
            )

        return read_artifact
