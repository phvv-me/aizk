import functools
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import FunctionTool
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth, export, graph, retrieval
from ..background import tasks_overview
from ..config import settings
from ..eval import Budget, SweepMatrix, benchmarks, run_eval, run_scale_benchmark, run_sweep
from ..exceptions import NotGroupAdminError
from ..extract import ingest as extract_ingest
from ..store import Group, Principal, acting_as, system_session
from .middleware import AnonymousRateLimit, PrincipalMiddleware
from .principal import ADMIN_TAG, caller_principal, require_admin, require_identified


class AizkMCP(FastMCP):
    """FastMCP wired for aizk, with `admin_tool` beside `tool` for the operational surface.

    `__init__` wires the principal-resolving middleware, the anonymous rate limit on a shared HTTP
    transport, and a token verifier when one is configured. `admin_tool` reads the same as the
    inherited `tool` decorator at every call site, `@server.tool` for the three memory verbs and
    `@server.admin_tool` for the operational surface, the latter running the body behind
    `require_admin` and carrying `ADMIN_TAG`, the tag `PrincipalMiddleware.on_list_tools` hides
    from a non-admin listing.
    """

    def __init__(self, name: str) -> None:
        active_verifier = auth.verifier()
        if active_verifier:
            super().__init__(name, auth=active_verifier)
        else:
            super().__init__(name)
        self.add_middleware(PrincipalMiddleware())
        if settings.mcp_http:
            # a shared HTTP transport may serve unauthenticated strangers reading public groups,
            # so their tool calls consume from a token bucket while an authenticated caller passes.
            self.add_middleware(
                AnonymousRateLimit(max_requests_per_second=settings.anon_rate_per_second)
            )

    def admin_tool[**P, T](self, fn: Callable[P, Awaitable[T]]) -> FunctionTool:
        """Register `fn` as an admin-only tool, gated by `require_admin` and tagged `ADMIN_TAG`.

        Applies `ADMIN_TAG` so `PrincipalMiddleware.on_list_tools` hides the tool from a non-admin
        listing, and wraps the body so every call, protocol-routed or a direct `tool.run()`,
        resolves the caller and refuses a non-admin before the body ever runs.

        fn: the admin tool body to wrap and register.
        """

        @functools.wraps(fn)
        async def gated(*args: P.args, **kwargs: P.kwargs) -> T:
            await require_admin()
            return await fn(*args, **kwargs)

        return self.tool(gated, tags={ADMIN_TAG})


server = AizkMCP("aizk")


async def resolve_scope(scope: str | None, principal_id: uuid.UUID) -> uuid.UUID | None:
    """Resolve a scope name to the group id rows are shared with, null for a private scope.

    A null name means the memory is private to the caller, so null is returned unchanged. Otherwise
    the visible group of that name is looked up and its id returned, and an unknown name is a fail
    fast rather than a silent private write.

    scope: human-readable group name, null for private.
    principal_id: identity whose visibility scopes the group lookup.
    """
    if scope is None:
        return None
    async with acting_as(principal_id) as session:
        group = await Group.named(session, scope)
    return group.id


@server.tool
async def recall(query: str, scope: str | None = None, k: int = 8) -> str:
    """Recall the most relevant memory for a query as compact facts and source snippets.

    query: what to recall context about.
    scope: group name to narrow the read to that group's composed graph, the whole visible
        union of private and every member and public scope otherwise.
    k: how many hits and seed facts to surface.
    """
    principal_id = await caller_principal()
    scope_id = await resolve_scope(scope, principal_id)
    result = await retrieval.recall(query, principal_id=principal_id, k=k, scope=scope_id)
    return result.render()


@server.tool
async def remember(text: str, scope: str | None = None, kind: str = "note") -> str:
    """Remember a piece of text as working memory, the cheap capture recall reads at once.

    The write lands in the fast session tier as one embedded row rather than paying the chunk,
    embed, and extract pipeline up front, so a capture is immediate and a recall folds it in
    beside the graph. The autonomous promotion pass later moves the aged or overflow items into
    the long-term graph through the extract-and-consolidate pipeline.

    text: the content to remember.
    scope: group name to share it with, private to the caller when null.
    kind: coarse type tag, such as note or code.
    """
    principal_id = await require_identified()
    scope_id = await resolve_scope(scope, principal_id)
    item_id = await extract_ingest.remember_session(
        text, kind=kind, owner_id=principal_id, scope=scope_id
    )
    return str(item_id)


@server.tool
async def get_context(
    query: str, scope: str | None = None, token_budget: int | None = None
) -> str:
    """Assemble a token-budgeted, prompt-ready context pack for a query, the one lane mix.

    Recalls for the query and packs the fused profiles, community and RAPTOR summaries, facts,
    and still-working session items into a compact pack that fits the token budget, the broad
    view first and the raw sources last, so an agent reads one ready context without choosing
    the mix. The pack reuses recall under the caller's own visibility.

    query: what to assemble context about.
    scope: group name to narrow the read to that group's composed graph, the whole visible
        union otherwise.
    token_budget: token ceiling the pack fits within, the configured default when null.
    """
    principal_id = await caller_principal()
    scope_id = await resolve_scope(scope, principal_id)
    return await retrieval.assemble_context_pack(
        query, principal_id=principal_id, token_budget=token_budget, scope=scope_id
    )


@server.tool
async def reference(uri: str, scope: str | None = None) -> str:
    """Record a reference to a paper, url, or file so it is recallable later.

    uri: locator of the paper, url, or file.
    scope: group name to share it with, private to the caller when null.
    """
    principal_id = await require_identified()
    scope_id = await resolve_scope(scope, principal_id)
    document_id = await extract_ingest.record_reference(uri, owner_id=principal_id, scope=scope_id)
    return str(document_id)


async def resolve_group_admin(session: AsyncSession, group: str) -> Group:
    """Resolve a group name and refuse the call unless the caller administers it, group back.

    Shared by the curation tools: resolves the scope name, then checks the caller holds the
    group's own admin membership role or the server-wide admin flag, raising a `ToolError` a
    non-admin caller reads plainly rather than the domain `NotGroupAdminError` it wraps. `Group`
    and `Membership` carry no row level security of their own, so reading and checking them
    through the system-acting session is exactly as visible as through the caller's own.

    session: open session, already acting as the system principal.
    group: name of the curated group the call would administer.
    """
    principal_id = await caller_principal()
    group_row = await Group.named(session, group)
    try:
        await group_row.require_admin(session, principal_id)
    except NotGroupAdminError as error:
        raise ToolError(str(error)) from error
    return group_row


def parse_fact_ids(facts: str) -> list[uuid.UUID]:
    """Parse a comma-separated fact id list into uuids, ignoring stray whitespace.

    facts: comma-separated fact ids, as a group-admin tool call receives them.
    """
    return [uuid.UUID(fact.strip()) for fact in facts.split(",") if fact.strip()]


@server.tool
async def pending(group: str) -> str:
    """List a curated group's unreviewed facts awaiting a group admin's approval.

    A pending fact is invisible to everyone but its own author until it is approved or rejected,
    so this is the one place a group admin sees the whole review queue at once.

    group: name of the curated group whose pending facts are listed.
    """
    async with system_session() as session:
        group_row = await resolve_group_admin(session, group)
        facts = await group_row.pending_facts(session)
    return (
        "\n".join(f"{fact.id} by {fact.owner_id}: {fact.statement}" for fact in facts)
        or "no pending facts"
    )


@server.tool
async def approve(group: str, facts: str = "all") -> str:
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
    return f"approved {count} facts in {group!r}"


@server.tool
async def reject(group: str, facts: str) -> str:
    """Reject a curated group's pending facts, discarding them before they ever became canonical.

    group: name of the curated group the facts belong to.
    facts: comma-separated fact ids to reject.
    """
    async with system_session() as session:
        group_row = await resolve_group_admin(session, group)
        count = await group_row.reject_facts(session, parse_fact_ids(facts))
    return f"rejected {count} facts in {group!r}"


@server.admin_tool
async def force_rebuild(limit: int | None = None, source: str | None = None) -> str:
    """Force the graph build now over the admin's pending chunks, the on-demand extraction.

    Runs inline rather than waiting for a worker to drain the queue, so an admin gets the
    entities and facts immediately. The autonomous default is the queue the worker drains.

    limit: maximum number of chunks to process, all of them when null.
    source: restrict the build to chunks of documents whose title matches this substring.
    """
    principal_id = await caller_principal()
    entities, facts = await graph.build_graph(
        limit=limit, principal_id=principal_id, source=source
    )
    return f"created {entities} entities and {facts} facts"


@server.admin_tool
async def force_decay(half_life_days: float = 90.0) -> str:
    """Force the decay pass now, archiving stale facts that leave recall but stay in history.

    half_life_days: age in days at which an unaccessed fact's relevance halves.
    """
    principal_id = await caller_principal()
    archived = await graph.decay(principal_id=principal_id, half_life_days=half_life_days)
    return f"archived {archived} stale facts"


@server.admin_tool
async def force_reembed() -> str:
    """Force a re-embed of every visible stored vector with the current embedder, a migration.

    Re-encodes the chunk, entity, fact, community, and profile embeddings from their stored
    source text, so switching the embed backend or model needs no re-ingest.
    """
    principal_id = await caller_principal()
    written = await graph.reembed(principal_id=principal_id)
    return f"re-embedded {written} vectors"


@server.admin_tool
async def force_raptor() -> str:
    """Force the RAPTOR tree build now, the recursive summary tiers above the communities.

    Rebuilds the admin's tree inline rather than waiting for the weekly pass, clustering the
    communities up level by level into the summary-of-summaries a broad query reads. Build the
    communities first, since the tree climbs above them.
    """
    principal_id = await caller_principal()
    written = await graph.build_raptor(principal_id=principal_id)
    return f"built {written} raptor summaries"


@server.admin_tool
async def bench(questions_file: str | None = None, k: int = 8) -> str:
    """Run the eval harness over visible memory and report hit-at-k with a per-config split.

    questions_file: a file of one question per line, or null to synthesize them from facts.
    k: how many hits and seed facts each recall surfaces.
    """
    principal_id = await caller_principal()
    questions = (
        Path(questions_file).read_text(encoding="utf-8").splitlines() if questions_file else None
    )
    report = await run_eval(questions, k=k, principal_id=principal_id)
    head = (
        f"n={report.n} hit@{k}={round(report.hit_at_k, 3)} "
        f"ndcg@{k}={round(report.ndcg_at_k, 3)} mrr={round(report.mrr, 3)} "
        f"judge={report.mean_judge}"
    )
    rows = "\n".join(f"  {config}: {round(hit, 3)}" for config, hit in report.per_config.items())
    return f"{head}\n{rows}" if rows else head


@server.admin_tool
async def sweep(
    questions_file: str | None = None,
    k: int = 8,
    dims: str | None = None,
) -> str:
    """Sweep the config grid and report quality, latency, and memory for each config.

    Ranges recall over the rerank, ppr, and query-routing toggles by default, widened by the
    comma-separated Matryoshka widths when given, and reports recall@k, ndcg@k, and mrr with a
    significance table alongside the per-config latency and memory the mainboard meter measured,
    the demonstration of quality and cost.

    questions_file: a file of one question per line, or null to synthesize them from facts.
    k: how many hits and seed facts each recall surfaces.
    dims: comma-separated Matryoshka widths to sweep, such as `512,1024,2048`, the live width
        when null, noting a width past the stored one needs a re-embedded corpus to score.
    """
    principal_id = await caller_principal()
    questions = (
        Path(questions_file).read_text(encoding="utf-8").splitlines() if questions_file else None
    )
    matrix = SweepMatrix(embed_dim=[int(dim) for dim in dims.split(",")] if dims else [])
    report = await run_sweep(questions, k=k, principal_id=principal_id, matrix=matrix)
    return report.render()


@server.admin_tool
async def benchmark(name: str, dataset_path: str, k: int = 8) -> str:
    """Sweep the config grid over one external 2026 benchmark loaded from its dataset file.

    Loads the named benchmark, EverMemBench or TEMPO, into the harness gold and runs the same
    config sweep over it, so the report shows quality, latency, and memory on a public dataset
    rather than only the synthesized corpus probe. Gated by `benchmarks_enabled` since the
    datasets are an optional dev download.

    name: which benchmark to load, `evermembench` or `tempo`.
    dataset_path: path to the benchmark's JSONL file.
    k: how many hits and seed facts each recall surfaces.
    """
    principal_id = await caller_principal()
    if not settings.benchmarks_enabled:
        raise ToolError("aizk benchmarks are off, set AIZK_BENCHMARKS_ENABLED to run them")
    if name not in benchmarks.LOADERS:
        raise ValueError(
            f"unknown benchmark {name!r}, expected one of {sorted(benchmarks.LOADERS)}"
        )
    gold = benchmarks.benchmark_gold(benchmarks.LOADERS[name](Path(dataset_path)))
    report = await run_sweep(None, k=k, principal_id=principal_id, gold=gold)
    return report.render()


@server.admin_tool
async def scale(
    sizes: str = "1000,10000",
    k: int = 8,
    repeats: int = 10,
    recall_p95_ms: float = 200.0,
) -> str:
    """Grow a throwaway corpus through the sizes and report the scaling curve with each knee.

    Ingests a synthetic corpus at increasing sizes under a throwaway principal, then at each
    size measures recall latency percentiles with a per-lane breakdown, ingestion throughput,
    the pagerank and community-detection graph ops, and the storage and index footprint,
    flagging the first size each component crossed its budget so the report names where the
    Postgres-CTE or cuGraph rewrite pays. The throwaway principal and its rows are then purged.

    sizes: comma-separated corpus chunk counts to measure, such as `1000,10000,100000`, the
        hundred-thousand and million points left opt-in since one run writes that many rows.
    k: how many hits and seed facts each recall surfaces.
    repeats: how many recall and per-lane calls each percentile is read over.
    recall_p95_ms: the tail recall budget in milliseconds the recall knee is flagged against.
    """
    report = await run_scale_benchmark(
        sizes=tuple(int(size) for size in sizes.split(",")),
        k=k,
        repeats=repeats,
        budget=Budget(recall_p95_ms=recall_p95_ms),
    )
    return report.render()


@server.admin_tool
async def ingest(path: str, scope: str | None = None) -> str:
    """Ingest a file or directory of notes and code into memory and return the document count.

    Code files are chunked AST-aware and stamped `kind=code`, notes flow through the prose
    splitter, and a file whose content hash already exists is skipped.

    path: file or directory to ingest.
    scope: group name to share it with, private to the caller when null.
    """
    principal_id = await caller_principal()
    scope_id = await resolve_scope(scope, principal_id)
    count = await extract_ingest.ingest_path(Path(path), owner_id=principal_id, scope=scope_id)
    return f"ingested {count} documents from {path}"


@server.admin_tool
async def ingest_image(path: str, caption: str | None = None, scope: str | None = None) -> str:
    """Ingest an image into memory in the shared space so a text query can recall it.

    The image embeds through the served multimodal model's image lane into the same space the
    text chunks live in, stamped `kind=image`, so the endpoint at `embed_url` must serve a
    multimodal embedding model and a text-only one fails fast.

    path: image file to ingest.
    caption: text stored on the chunk and shown in recall, the file name when null.
    scope: group name to share it with, private to the caller when null.
    """
    principal_id = await caller_principal()
    scope_id = await resolve_scope(scope, principal_id)
    document_id = await extract_ingest.ingest_image(
        Path(path), caption=caption, owner_id=principal_id, scope=scope_id
    )
    return str(document_id)


@server.admin_tool
async def promote(document: str, to_scope: str) -> str:
    """Promote a document and its chunks and facts into a wider scope as a new audited copy.

    A deliberate admin governance write, never autonomous, so widening a memory's visibility
    always passes through a human admin.

    document: id of the source document to promote.
    to_scope: name of the target group the copy is published into.
    """
    principal_id = await caller_principal()
    count = await graph.promote(uuid.UUID(document), to_scope, principal_id=principal_id)
    return f"promoted {count} rows into {to_scope}"


@server.admin_tool
async def export_scope(path: str) -> str:
    """Export the admin's visible memory to a JSONL file, the principal-scoped portable dump.

    Writes every document, chunk, entity, and fact the admin can see, the facts carrying both
    their valid-time and transaction-time windows so the bi-temporal history rides along, one
    json object per line tagged with its table. The dump runs under the admin's own row level
    security, so exactly the rows that principal may see leave and no other tenant's do.
    Import-from-others is out of scope, this only emits.

    path: the JSONL file the dump is written to.
    """
    principal_id = await caller_principal()
    report = await export.export_scope(Path(path), principal_id=principal_id)
    return (
        f"exported {report.documents} documents, {report.chunks} chunks, "
        f"{report.entities} entities, {report.facts} facts to {report.path}"
    )


@server.admin_tool
async def tasks_status() -> str:
    """Report the autonomous engine's pending, running, failed, last-run, and lag counts.

    Reads the queue tables for what is waiting and in flight, what failed and when the last job
    ran, and the embed-to-extract lag, the chunks queued for extraction but not yet processed.
    """
    status = await tasks_overview()
    return (
        f"pending={status.pending} running={status.running} failed={status.failed} "
        f"lag={status.lag} last_run={status.last_run or 'never'}"
    )


@server.admin_tool
async def create_user(name: str) -> str:
    """Create a regular, non-admin principal and return its id, the multi-user onboarding tool.

    name: human-readable display name for the new actor.
    """
    async with system_session() as session:
        principal = await Principal.create(session, name)
    return str(principal.id)


@server.admin_tool
async def grant_admin(principal: str) -> str:
    """Promote a principal to admin so it manages the operational surface.

    principal: id of the principal to grant administrator standing.
    """
    async with system_session() as session:
        target = await session.get(Principal, uuid.UUID(principal))
        if target is None:
            raise ToolError(f"no principal {principal!r}")
        await target.grant_admin(session)
    return f"{principal} is now an admin"


@server.admin_tool
async def create_group(name: str, public: bool = False, curated: bool = False) -> str:
    """Create a sharing group and return its id, the scope memberships and promotions target.

    The caller founds the group and joins it as its admin member, so it can write into the new
    scope and review its pending canon immediately.

    name: unique human-readable label for the group.
    public: whether the group's rows are readable by anyone from the start, members-only
        when false.
    curated: whether a write into this group's canon must clear group-admin review through
        `pending` and `approve` before it becomes visible to the rest of the group, immediate
        when false.
    """
    creator = await caller_principal()
    async with system_session() as session:
        group_row = await Group.create(
            session, name, public=public, curated=curated, creator=creator
        )
        group_id = group_row.id
    return str(group_id)


@server.admin_tool
async def add_member(principal: str, group: str, role: str = "writer") -> str:
    """Add a principal to a group so that group's scope becomes visible to it under RLS.

    principal: id of the principal joining the group.
    group: name of the group the principal joins.
    role: standing within the group, reader for read-only visibility, writer or admin to
        also write into the shared scope.
    """
    async with system_session() as session:
        group_row = await Group.named(session, group)
        await group_row.add_member(session, uuid.UUID(principal), role=role)
    return f"{principal} added to {group!r} as {role}"


@server.admin_tool
async def remove_member(principal: str, group: str) -> str:
    """Remove a principal from a group, its scope no longer visible to them.

    principal: id of the principal leaving the group.
    group: name of the group the principal leaves.
    """
    async with system_session() as session:
        group_row = await Group.named(session, group)
        await group_row.remove_member(session, uuid.UUID(principal))
    return f"{principal} removed from {group!r}"


@server.admin_tool
async def publish_group(group: str, public: bool = True) -> str:
    """Publish a group so anyone can read its rows, or unpublish it back to members-only.

    The shared-brain switch: a published group's graph is readable by any caller, anonymous
    strangers included, while writing keeps requiring an explicit writer membership.

    group: name of the group to publish or unpublish.
    public: true to publish, false to make members-only again.
    """
    async with system_session() as session:
        group_row = await Group.named(session, group)
        await group_row.publish(session, public=public)
    state = "public" if public else "members-only"
    return f"{group!r} is now {state}"


@server.admin_tool
async def curate_group(group: str, curated: bool = True) -> str:
    """Curate or uncurate a group, flipping whether its writes must clear group-admin review.

    A curated group's writes land pending until a group admin approves them through `pending` and
    `approve`, the review loop that keeps the group's floor to verified knowledge; uncurating a
    group returns it to writing straight through like any ordinary shared scope.

    group: name of the group to curate or uncurate.
    curated: the new curation state, true to require review and false to write straight through.
    """
    async with system_session() as session:
        group_row = await Group.named(session, group)
        await group_row.curate(session, curated=curated)
    state = "curated" if curated else "uncurated"
    return f"{group!r} is now {state}"


@server.admin_tool
async def delete_group(group: str) -> str:
    """Delete a group, memberships cascading and its rows falling back to their owners.

    group: name of the group to delete.
    """
    async with system_session() as session:
        group_row = await Group.named(session, group)
        await group_row.delete(session)
    return f"{group!r} deleted"


@server.admin_tool
async def list_groups() -> str:
    """List every group with its visibility and member count, the sharing roster."""
    async with system_session() as session:
        groups = await Group.list_all(session)
    return (
        "\n".join(
            f"{g['name']} [{'public' if g['public'] else 'members-only'}] members={g['members']}"
            for g in groups
        )
        or "no groups"
    )


@server.admin_tool
async def list_principals() -> str:
    """List every principal known to the engine, the admin roster."""
    async with system_session() as session:
        principals = await Principal.list_all(session)
    return (
        "\n".join(f"{p.id} {p.display_name or 'unnamed'}" for p in principals) or "no principals"
    )


@server.admin_tool
async def audit(limit: int = 20) -> str:
    """List the most recent visible document writes with owner, scope, and promotion source.

    limit: maximum number of writes to return.
    """
    principal_id = await caller_principal()
    documents = await Principal.recent_writes(principal_id, limit=limit)
    return (
        "\n".join(
            f"{d.id} [{d.kind}] owner={d.owner_id} scope={d.scope or 'private'} "
            f"from={d.promoted_from or '-'} {d.title or 'untitled'}"
            for d in documents
        )
        or "no writes"
    )
