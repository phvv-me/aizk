import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from loguru import logger
from mainboard.profiling import enable_spans
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ops, retrieval
from ..config import settings
from ..exceptions import NotGroupAdminError
from ..extract import ingest as extract_ingest
from ..retrieval import ContextPack
from ..scopes import resolve_scopes
from ..store import Group, system_session
from ..store import Principal as PrincipalRow
from .middleware import AnonymousRateLimit, PrincipalMiddleware
from .models import PendingFact, ReviewResult, WriteResult
from .principal import current_principal, require_identified


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
    This server keeps only the client verbs, recall, remember, reference, and the group-curation
    trio, the last three gated on group-admin membership in-body rather than a server-wide role.
    `__init__` wires the principal-resolving middleware, the startup health-and-auto-setup
    lifespan, the anonymous rate limit on a shared HTTP transport, and a token verifier when one
    is configured.
    """

    def __init__(self, name: str) -> None:
        active_verifier = PrincipalRow.verifier()
        if active_verifier:
            super().__init__(name, auth=active_verifier, lifespan=startup_check)
        else:
            super().__init__(name, lifespan=startup_check)
        self.add_middleware(PrincipalMiddleware())
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
    principal = current_principal()
    lens = await resolve_scopes(scopes, principal.id)
    return await retrieval.assemble_context_pack(
        query, principal_id=principal.id, token_budget=budget, scopes=lens
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
    principal = require_identified(current_principal())
    target = await resolve_scopes(scopes, principal.id)
    item_id = await extract_ingest.remember_session(
        text, kind=kind, owner_id=principal.id, scopes=target
    )
    return WriteResult(id=item_id)


@server.tool
async def reference(uri: str, scopes: str | None = None) -> WriteResult:
    """Record a reference to a paper, url, or file so it is recallable later.

    uri: locator of the paper, url, or file.
    scopes: comma-separated group names to share it with, private to the caller when null.
    """
    principal = require_identified(current_principal())
    target = await resolve_scopes(scopes, principal.id)
    document_id = await extract_ingest.record_reference(uri, owner_id=principal.id, scopes=target)
    return WriteResult(id=document_id)


async def resolve_group_admin(session: AsyncSession, group: str) -> Group:
    """Resolve a group name and refuse the call unless the caller administers it, group back.

    Shared by the curation verbs, it resolves the scope name, then checks the caller holds the
    group's own admin membership role or the server-wide admin flag, raising a `ToolError` a
    non-admin caller reads plainly rather than the domain `NotGroupAdminError` it wraps. `Group`
    and `Membership` carry no row level security of their own, so reading and checking them
    through the system-acting session is exactly as visible as through the caller's own.

    session: open session, already acting as the system principal.
    group: name of the curated group the call would administer.
    """
    principal = current_principal()
    group_row = await Group.named(session, group)
    try:
        await group_row.require_admin(session, principal.id)
    except NotGroupAdminError as error:
        raise ToolError(str(error)) from error
    return group_row


def parse_fact_ids(facts: str) -> list[uuid.UUID]:
    """Parse a comma-separated fact id list into uuids, ignoring stray whitespace.

    facts: comma-separated fact ids, as a curation verb receives them.
    """
    return [uuid.UUID(fact.strip()) for fact in facts.split(",") if fact.strip()]


@server.tool
async def pending(group: str) -> list[PendingFact]:
    """List a curated group's unreviewed facts awaiting a group admin's approval.

    A pending fact is invisible to everyone but its own author until it is approved or rejected,
    so this is the one place a group admin sees the whole review queue at once.

    group: name of the curated group whose pending facts are listed.
    """
    async with system_session() as session:
        group_row = await resolve_group_admin(session, group)
        facts = await group_row.pending_facts(session)
    return [
        PendingFact(id=f.id, owner_id=f.owner_id, predicate=f.predicate, statement=f.statement)
        for f in facts
    ]


@server.tool
async def approve(group: str, facts: str = "all") -> ReviewResult:
    """Approve a curated group's pending facts, the review that grows its verified canon.

    An approved fact joins the group's visible floor for every member and public reader at once,
    the moment a pending write becomes canonical knowledge rather than one author's claim.

    group: name of the curated group the facts belong to.
    facts: comma-separated fact ids to approve, or "all" for every still-pending fact.
    """
    async with system_session() as session:
        group_row = await resolve_group_admin(session, group)
        ids = None if facts == "all" else parse_fact_ids(facts)
        count = await group_row.approve_facts(session, ids)
    return ReviewResult(group=group, count=count)


@server.tool
async def reject(group: str, facts: str) -> ReviewResult:
    """Reject a curated group's pending facts, discarding them before they ever became canonical.

    group: name of the curated group the facts belong to.
    facts: comma-separated fact ids to reject.
    """
    async with system_session() as session:
        group_row = await resolve_group_admin(session, group)
        count = await group_row.reject_facts(session, parse_fact_ids(facts))
    return ReviewResult(group=group, count=count)
