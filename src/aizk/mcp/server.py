from datetime import datetime
from functools import cache
from typing import Annotated, Self

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from pydantic import UUID7, Field, StringConstraints

from .. import graph, retrieval
from ..background.queue import enqueue_document
from ..config import settings
from ..extract import ingest as extract_ingest
from ..provenance import CaptureContext
from ..retrieval import RecallResult
from ..store.identity import User
from ..types import ScopeNames
from .auth import Auth
from .middleware import CallerRateLimit, IdentityMiddleware, bound_user
from .models import ShareResult, WriteResult

type _RecallQuery = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=settings.mcp_recall_query_max_chars
    ),
]
type _RememberText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=settings.mcp_remember_max_chars
    ),
]
type _SourceURI = Annotated[str, StringConstraints(max_length=settings.mcp_source_uri_max_chars)]
type _ScopeNames = Annotated[ScopeNames, Field(max_length=settings.mcp_scope_names_max)]
type _Documents = Annotated[
    list[UUID7], Field(min_length=1, max_length=settings.mcp_share_documents_max)
]
type _RecallBudget = Annotated[int, Field(gt=0, le=settings.mcp_recall_budget_max_tokens)]


class AizkMCP(FastMCP):
    """Expose Aizk's authenticated memory tools through FastMCP.

    Identity middleware resolves one Logto-backed `User` before each call. A
    per-caller token bucket limits sustained work, and PostgreSQL row security
    remains the final authorization boundary for every retrieved or written row.
    """

    def __init__(self, name: str) -> None:
        self.authentication = Auth()
        super().__init__(name, auth=self.authentication.provider())
        self.add_middleware(IdentityMiddleware(self.authentication))
        self.add_middleware(
            CallerRateLimit(max_requests_per_second=settings.mcp_request_rate_per_second)
        )

    @classmethod
    @cache
    def shared(cls) -> Self:
        """Build the process-wide MCP application only when the serving command needs it."""
        application = cls("aizk")
        for tool in (status, recall, remember, share):
            application.tool(tool)
        return application

    async def user(self, context: Context, identified: bool = False) -> User:
        """Return the request's resolved caller and optionally require authentication."""
        if (user := await bound_user(context)) is None:
            raise ToolError("no user resolved for this call")
        if identified and user.is_anonymous():
            raise ToolError("anonymous callers are read-only, authenticate to write")
        return user


async def status(context: Context) -> User:
    """Return the caller's organizations, roles, and permissions as supplied by Logto."""
    return await AizkMCP.shared().user(context, identified=True)


async def recall(
    query: _RecallQuery,
    context: Context,
    budget: _RecallBudget = settings.context_token_budget,
) -> str:
    """Return visible evidence for one question as clear, ordered Markdown.

    query: natural-language question whose length is bounded by deployment settings.
    budget: optional evidence cap. Omit it unless repeated responses are too long.
    """
    if not (query := query.strip()):
        raise ToolError("recall query cannot be blank")
    user = await AizkMCP.shared().user(context)
    candidates = await retrieval.recall(query, user, token_budget=budget)
    scope_details = {user.id: RecallResult.Scope(name="private")} | {
        organization.id: RecallResult.Scope(
            name=organization.name,
            description=organization.description,
        )
        for organization in user.organizations
    }
    return RecallResult.from_candidates(candidates, scope_details).to_markdown()


async def remember(
    text: _RememberText,
    context: Context,
    source_uri: _SourceURI | None = None,
    observed_at: datetime | None = None,
    expires_at: datetime | None = None,
    scopes: _ScopeNames | None = None,
) -> WriteResult:
    """Store one source document and enqueue its derived graph projection.

    text: self-describing Markdown or plain text that remains the source authority.
    source_uri: original website or paper PDF URL. Omit it for authored notes.
    observed_at: optional time when the statement became applicable. Normally omitted.
    expires_at: known time after which the statement stops being true. It is not a reminder.
        Normally omitted.
    scopes: optional authorized Logto organization names. Omission means private memory.
    """
    if not text.strip():
        raise ToolError("memory text cannot be blank")
    try:
        declaration = extract_ingest.SourceDeclaration.from_text(text)
    except ValueError as invalid:
        raise ToolError(str(invalid)) from invalid
    user = await AizkMCP.shared().user(context, identified=True)
    target = user.write_scope(scopes)
    try:
        document_id = await extract_ingest.ingest_text(
            user,
            text,
            title=declaration.title,
            source_uri=source_uri,
            created_by=user.id,
            scopes=target,
            capture=CaptureContext(
                speaker_label=user.label,
                observed_at=observed_at,
                expires_at=expires_at,
            ),
        )
    except ValueError as invalid:
        raise ToolError(str(invalid)) from invalid
    if document_id is None:
        raise ToolError("memory ingestion did not create a document")
    await enqueue_document(document_id, target)
    return WriteResult(id=document_id)


async def share(
    documents: _Documents, context: Context, scopes: _ScopeNames | None = None
) -> ShareResult:
    """Copy visible documents into one authorized destination without moving sources.

    documents: visible document IDs to copy, bounded per call.
    scopes: optional authorized Logto organization names. Omission means private memory.
    """
    user = await AizkMCP.shared().user(context, identified=True)
    shared = await graph.promote(documents, user.write_scope(scopes), user)
    return ShareResult(shared=shared)
