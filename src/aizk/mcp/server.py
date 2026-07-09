import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from loguru import logger
from mainboard.profiling import enable_spans

from .. import ops, retrieval
from ..config import settings
from ..extract import ingest as extract_ingest
from ..retrieval import ContextPack
from ..scopes import resolve_scopes
from ..store import Document, Membership, acting_as
from ..store import User as UserRow
from ..store.engine import session
from .middleware import AnonymousRateLimit, IdentityMiddleware
from .models import MoveResult, WriteResult
from .user import current_user, require_identified


@asynccontextmanager
async def startup_check(mcp: FastMCP) -> AsyncIterator[None]:
    """Ensure the database is ready before serving, auto-applying setup when it falls behind.

    Runs the same health read the `aizk health` CLI command exposes once at process start, and a
    schema behind head runs `ops.setup` inline and logs what it applied, so the server comes up
    ready with no manual migrate step. Skipped when `settings.auto_setup` is off, the
    manual-control opt-out for a deployment that runs its own migrations.

    mcp: the server this lifespan manages, part of fastmcp's fixed lifespan signature.
    """
    if settings.profiling:
        enable_spans()
    if settings.auto_setup:
        report = await ops.health()
        if not report.migration.up_to_date:
            applied = await ops.setup()
            logger.info("auto-setup migrated {} -> {}", applied.migrated_from, applied.migrated_to)
    yield


class AizkMCP(FastMCP):
    """FastMCP wired for aizk, serving only the client verbs a key-holder reaches.

    The operational surface, every maintenance, governance, and eval operation, lives in the
    `aizk` CLI, reached by ssh rather than over the network, so a leaked key can never drive it.
    This server keeps only the client verbs, recall, remember, reference, and move.
    `__init__` wires the user-resolving middleware, the startup health-and-auto-setup
    lifespan, the anonymous rate limit on a shared HTTP transport, and a token verifier when one
    is configured.
    """

    def __init__(self, name: str) -> None:
        auth = UserRow.auth_provider()
        if auth:
            super().__init__(name, auth=auth, lifespan=startup_check)
        else:
            super().__init__(name, lifespan=startup_check)
        self.add_middleware(IdentityMiddleware())
        if settings.mcp_http:
            # a shared HTTP transport may serve unauthenticated strangers reading public groups,
            # so their tool calls consume from a token bucket while an authenticated caller passes.
            self.add_middleware(
                AnonymousRateLimit(max_requests_per_second=settings.anon_rate_per_second)
            )


server = AizkMCP("aizk")


@server.tool
async def recall(query: str, scopes: str | None = None, budget: int | None = None) -> ContextPack:
    """Recall everything the memory holds on a question as one ready, ranked context pack.

    The single retrieval verb. Ask a specific natural-language question, such as "what are the
    ongoing projects", "what is the SPReAD project about", or "what changed in finances lately",
    and the server does the whole retrieval itself. It fuses reranked source passages, the matching
    latest facts and their graph neighbors, the personalized-pagerank reach, the community and
    RAPTOR summaries, and any still-working session items into token-budgeted blocks, the broad
    view first and the raw sources last. One call returns a pack ready to reason over, with no lane
    to choose and no follow-up read. Ask a sharper question rather than a second call.

    query: the question to pull context for, natural language, as specific as you can make it.
    scopes: comma-separated group names narrowing the read to that combination's composed graph
        (`"finance,business"` reads only what is scoped to exactly that pair together), the whole
        visible union of private and every member and public scope otherwise. Naming any scopes
        excludes the caller's own private notes, which surface only when scopes is left null.
    budget: token ceiling the pack fits within, the configured default when null.
    """
    user = current_user()
    lens = await resolve_scopes(scopes, user.id)
    return await retrieval.assemble_context_pack(
        query, user_id=user.id, token_budget=budget, scopes=lens
    )


@server.tool
async def remember(text: str, scopes: str | None = None, kind: str = "note") -> WriteResult:
    """Remember a piece of text as working memory, the cheap capture recall reads at once.

    The write lands in the fast session tier as one embedded row rather than paying the chunk,
    embed, and extract pipeline up front, so a capture is immediate and a recall folds it in
    beside the graph. The autonomous promotion pass later moves the aged or overflow items into
    the long-term graph through the extract-and-consolidate pipeline.

    text: the content to remember.
    scopes: comma-separated group names to share it with (`"finance,business"` lands one claim
        visible only to a caller standing in every one of those groups), private to the caller
        when null.
    kind: coarse type tag, such as note or code.
    """
    user = require_identified(current_user())
    target = await resolve_scopes(scopes, user.id)
    item_id = await extract_ingest.remember_session(
        text, kind=kind, owner_id=user.id, scopes=target
    )
    return WriteResult(id=item_id)


@server.tool
async def reference(uri: str, scopes: str | None = None) -> WriteResult:
    """Record a reference to a paper, url, or file so it is recallable later.

    uri: locator of the paper, url, or file.
    scopes: comma-separated group names to share it with, private to the caller when null.
    """
    user = require_identified(current_user())
    target = await resolve_scopes(scopes, user.id)
    document_id = await extract_ingest.record_reference(uri, owner_id=user.id, scopes=target)
    return WriteResult(id=document_id)


@server.tool
async def move(documents: str, scopes: str) -> MoveResult:
    """Move your own notes into a group scope, carrying each document's chunks and facts with it.

    Re-scopes whole documents you own, the source rows and the claims mined from them travelling
    together, so a note recalled after the move reads only under its new scope. Moving into a group
    needs writer or admin standing in every named group, and only documents you own move, so
    another member's contribution is never re-scoped from under them. Naming no scopes moves the
    documents back to private to you.

    documents: comma-separated document ids to move, as recall reports them in its source blocks.
    scopes: comma-separated group names to move them into, blank to make them private again.
    """
    user = require_identified(current_user())
    target = await resolve_scopes(scopes, user.id)
    document_ids = parse_ids(documents)
    async with acting_as(user.id):
        writable = set((await session().scalars(Membership.writable_group_ids(user.id))).all())
        if not set(target) <= writable:
            raise ToolError("move needs writer or admin standing in every target group")
        moved = await Document.move_to_scope(user.id, document_ids, target)
    return MoveResult(moved=moved, scopes=scopes or "")


def parse_ids(raw: str) -> list[uuid.UUID]:
    """Parse a comma-separated id list into uuids, a malformed id a clean ToolError not a 500.

    raw: comma-separated uuids, as a verb receives its document or fact ids.
    """
    try:
        return [uuid.UUID(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError as error:
        raise ToolError(f"malformed id in {raw!r}") from error
