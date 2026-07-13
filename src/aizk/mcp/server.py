import uuid

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context

from .. import graph, retrieval
from ..config import settings
from ..extract import ingest as extract_ingest
from ..provenance import CaptureContext
from ..retrieval import Candidate
from ..store.identity import User
from ..types import ScopeNames
from .auth import Auth
from .middleware import AnonymousRateLimit, IdentityMiddleware, bound_user
from .models import ShareResult, WriteResult


class AizkMCP(FastMCP):
    """FastMCP wired for aizk, serving only the client verbs a key-holder reaches."""

    def __init__(self, name: str) -> None:
        self.authentication = Auth()
        super().__init__(name, auth=self.authentication.provider())
        self.add_middleware(IdentityMiddleware(self.authentication))
        self.add_middleware(
            AnonymousRateLimit(max_requests_per_second=settings.anon_rate_per_second)
        )

    async def user(self, context: Context, identified: bool = False) -> User:
        """Return the request's resolved caller and optionally require authentication."""
        if (user := await bound_user(context)) is None:
            raise ToolError("no user resolved for this call")
        if identified and user.is_anonymous():
            raise ToolError("anonymous callers are read-only, authenticate to write")
        return user


server = AizkMCP("aizk")


@server.tool
async def recall(query: str, context: Context, budget: int | None = None) -> tuple[Candidate, ...]:
    """Recall everything the memory holds on a question as ready, ranked evidence lines."""
    if not (query := query.strip()):
        raise ToolError("recall query cannot be blank")
    return await retrieval.recall(query, await server.user(context), token_budget=budget)


@server.tool
async def remember(
    text: str, context: Context, scopes: ScopeNames | None = None, kind: str = "note"
) -> WriteResult:
    """Remember a piece of text as working memory, the cheap capture recall reads at once."""
    user = await server.user(context, identified=True)
    item_id = await extract_ingest.remember_session(
        user,
        text,
        kind=kind,
        created_by=user.id,
        scopes=user.write_scope(scopes),
        capture=CaptureContext(speaker_label=user.label),
    )
    return WriteResult(id=item_id)


@server.tool
async def reference(uri: str, context: Context, scopes: ScopeNames | None = None) -> WriteResult:
    """Record a reference to a paper, url, or file so it is recallable later."""
    user = await server.user(context, identified=True)
    document_id = await extract_ingest.record_reference(
        user, uri, created_by=user.id, scopes=user.write_scope(scopes)
    )
    return WriteResult(id=document_id)


@server.tool
async def share(
    documents: list[uuid.UUID], context: Context, scopes: ScopeNames | None = None
) -> ShareResult:
    """Share visible notes into one scope set as provenance-linked copies."""
    user = await server.user(context, identified=True)
    shared = await graph.promote(documents, user.write_scope(scopes), user)
    return ShareResult(shared=shared, scopes=tuple(scopes or ()))
