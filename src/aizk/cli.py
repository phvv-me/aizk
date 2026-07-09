import asyncio
import os
import sys
import uuid
from pathlib import Path

from cyclopts import App
from loguru import logger
from mainboard.profiling import enable_spans

from alembic import command

from . import admin, ops
from . import backup as backup_ops
from .background.queue import enqueue_pending, install_queue_schema
from .background.schedule import run_worker
from .config import settings
from .extract.ingest import ingest_text
from .retrieval import assemble_context_pack

# env var a Stop hook sets to the session transcript path, the same file Claude Code records the
# turn-by-turn session to, so capture-session can read it without any argument
TRANSCRIPT_ENV = "AIZK_SESSION_TRANSCRIPT"

# default recall when a SessionStart hook calls recall-context with no query, broad enough to pull
# the recent decisions, patterns, and gotchas worth having loaded before a task begins
PROJECT_CONTEXT_QUERY = "recent decisions, patterns, gotchas, and project context"

app = App(
    name="aizk",
    help="Process and bootstrap entrypoint for the aizk memory engine, whose verbs are MCP tools.",
)

# operator commands are grouped by noun into sub-apps, so `aizk <noun> <verb>` reads as the shape
# it is: `aizk user create`, `aizk group add-member`, `aizk graph rebuild`. The hook and serve
# entrypoints (worker, serve-mcp, recall-context, capture-session, profile-report) stay top-level.
user = App(name="user", help="Users: the actors, human or agent, that own and read memory.")
group = App(name="group", help="Groups: the sharing scopes memberships and promotions target.")
graph = App(name="graph", help="Graph maintenance: rebuild, decay, reembed, raptor, forget.")
ontology = App(name="ontology", help="Ontology: the entity types and relation predicates.")
data = App(name="data", help="Data: ingest, export, audit, and promote documents.")
db = App(name="db", help="Database and engine ops: setup, health, migrations, backup, restore.")
eval = App(name="eval", help="Evaluation: bench, sweep, benchmark, and scale the retrieval.")
for _sub in (user, group, graph, ontology, data, db, eval):
    app.command(_sub)


@db.command(name="migrate")
def migrate() -> None:
    """Apply database migrations up to head, the pre-auth bootstrap step `ops.setup` also runs.

    A thin wrapper over `ops.run_alembic`, kept as its own command since a fresh database has no
    admin user yet to call the MCP `setup` tool through.
    """
    ops.run_alembic(command.upgrade, ops.alembic_config(), "head")
    print("done")


@db.command(name="makemigrations")
def makemigrations(message: str) -> None:
    """Autogenerate a new database migration from the current model metadata.

    Diffs the live database, which should already be at head, against the ORM metadata and writes
    the resulting revision file, the same autogenerate `alembic revision --autogenerate` runs.

    message: short description for the revision, becomes its file slug and migration docstring.
    """
    ops.run_alembic(command.revision, ops.alembic_config(), message=message, autogenerate=True)
    print("done")


@db.command(name="check-rls")
async def check_rls() -> None:
    """Verify every scoped table forces row level security with the canonical scope policies.

    Exits non-zero and lists each regression when a scoped table has lost ENABLE, FORCE, the
    scope_read or scope_write policy, or a clause that no longer scopes by owner and membership, so
    the no-leak contract can be gated in CI and checked by hand against any live database.
    """
    violations = await ops.scoped_rls_violations()
    if violations:
        for reason in violations:
            print(reason)
        sys.exit(1)
    print("ok")


@app.command
async def worker(batch_size: int = settings.queue_batch_size) -> None:
    """Run the autonomous engine, the queue and the scheduler together, until interrupted.

    Drains the on-write extraction and profile jobs and fires the scheduled maintenance passes,
    decay, dedup, communities, RAPTOR, profile refresh, self-improve, session promotion, and
    insight, each fanning out one job per user under its own row level security scope, so a single
    `aizk worker` self-maintains.

    batch_size: maximum number of jobs dequeued per round, settings.queue_batch_size by default,
        sized above settings.graph_build_concurrency so the queue path actually keeps enough
        chunks in flight to saturate vLLM's continuous batching.
    """
    if settings.profiling:
        enable_spans()
    await run_worker(batch_size=batch_size)


@db.command(name="install-queue")
async def install_queue() -> None:
    """Install the pgqueuer schema as the owner and grant the app role access.

    A thin wrapper over the same `install_queue_schema` the MCP `setup` tool runs, kept as its own
    command for the pre-auth bootstrap case where no admin user exists to call it through yet.
    """
    await install_queue_schema()
    print("done")


@db.command(name="backup")
async def backup(path: str) -> None:
    """Dump the whole database to a portable archive at `path`, the durable snapshot of memory.

    Runs `pg_dump` through `settings.pg_client_launcher` so a compose deployment captures the
    database with the container's own version-matched binaries. Schedule it from cron for the
    automated half of the backup story, `restore` reads the archive back.

    path: the host file the archive is written to.
    """
    report = await backup_ops.backup_database(path)
    print(f"backed up {report.bytes} bytes to {report.path}")


@db.command(name="restore")
async def restore(path: str) -> None:
    """Load a backup archive back into the configured database, overwriting its current contents.

    Destructive, `pg_restore --clean` drops each object the archive recreates, so the database
    ends holding exactly the backup. For a non-destructive recovery drill, restore into a fresh
    scratch database instead through `backup.restore_database(path, database=...)`.

    path: the archive `backup` wrote.
    """
    report = await backup_ops.restore_database(path)
    print(f"restored {report.path} into {report.database}")


@app.command
async def serve_mcp() -> None:
    """Run the aizk MCP server, and the background worker beside it, one process, one event loop.

    The one interface through which every memory verb is reached, streamable HTTP when
    AIZK_MCP_HTTP is set and stdio otherwise. When `serve_with_worker` is on (the default), the
    pgqueuer worker that drains the queue and fires every scheduled pass, the auto-backup among
    them, runs gathered on the same loop, so a single container is the whole engine. Set it off to
    run the server alone beside a separate `aizk worker`. fastmcp is imported lazily here so the
    rest of the CLI stays usable without it installed.
    """
    from .mcp.server import server

    logger.info(
        "serving aizk mcp, http={}, worker={}", settings.mcp_http, settings.serve_with_worker
    )
    serving = (
        server.run_http_async(host=settings.mcp_host, port=settings.mcp_port)
        if settings.mcp_http
        else server.run_stdio_async()
    )
    if settings.serve_with_worker:
        await asyncio.gather(serving, run_worker())
    else:
        await serving


@app.command
async def recall_context(
    query: str | None = None,
    k: int = 8,
    user: uuid.UUID | None = None,
) -> None:
    """Recall memory and print it for a SessionStart hook to inject as context.

    This is the shell bridge the session hook calls, not a memory verb, so it stays in the CLI
    while the verbs live as MCP tools. With no query it recalls the recent project context,
    otherwise it recalls for the query, and the formatted facts and source snippets are printed to
    stdout where the hook captures them.

    query: what to recall, the recent project context when null.
    k: number of hits and seed facts to surface.
    user: identity whose visibility scopes the recall, the system user when null.
    """
    pack = await assemble_context_pack(
        query or PROJECT_CONTEXT_QUERY, user_id=user or settings.system_user_id, k=k
    )
    print(
        "\n".join(f"[{block.lane}] {block.line}" for block in pack.blocks) or "no context recalled"
    )


@app.command
async def capture_session(
    user: uuid.UUID | None = None,
) -> None:
    """Capture the session's decisions into memory for a Stop hook to run at the end of a session.

    This is the shell bridge the session hook calls, not a memory verb, so it stays in the CLI
    while the verbs live as MCP tools. It reads the transcript path from the environment, remembers
    its text as a document so its decisions, patterns, and gotchas are chunked and embedded, and
    enqueues graph extraction so the extractor turns them into facts. Without a transcript path it
    is a quiet no-op, so the hook is safe to run in any session.

    user: identity that owns the captured memory, the system user when null.
    """
    transcript = os.environ.get(TRANSCRIPT_ENV)
    if not transcript or not Path(transcript).is_file():
        print("no session transcript to capture")
        return
    user_id = user or settings.system_user_id
    text_content = Path(transcript).read_text(encoding="utf-8")

    document_id = await ingest_text(text_content, title=Path(transcript).stem, owner_id=user_id)
    await enqueue_pending(user_id=user_id)
    print(f"captured session into document {document_id}")


@eval.command(name="scale")
async def scale(
    sizes: str = "1000,10000",
    k: int = 8,
    repeats: int = 10,
    recall_p95_ms: float = 200.0,
) -> None:
    """Run the scale benchmark and print the scaling curve with the knee flagged per component.

    Grows a synthetic corpus through the sizes under a throwaway user, measures recall latency
    percentiles with a per-lane breakdown, ingestion throughput, the pagerank and community-detect
    graph ops, and the storage footprint at each size, then prints the curve and the first size
    each component broke its budget. The throwaway user and its rows are purged at the end.

    sizes: comma-separated corpus chunk counts to measure, the hundred-thousand point left opt-in.
    k: number of hits and seed facts each recall surfaces.
    repeats: how many recall and per-lane calls each percentile is read over.
    recall_p95_ms: the tail recall budget in milliseconds the recall knee is flagged against.
    """
    report = await admin.scale(
        sizes=tuple(int(size) for size in sizes.split(",")),
        k=k,
        repeats=repeats,
        recall_p95_ms=recall_p95_ms,
    )
    print(report.render())


@user.command(name="create")
async def create_user(name: str) -> None:
    """Create a user and print its id, the multi-user onboarding op.

    name: human-readable display name for the new actor.
    """
    user = await admin.create_user(name)
    print(user.id)


@user.command(name="link")
async def link_user(oidc_subject: str, name: str = "") -> None:
    """Bind an OIDC subject to a user and print its id, the identity-provider bridge.

    Provisions the user the human or machine presenting that subject's token acts as, so a named
    user exists before its first login. A regular user, never an admin, since engine admin is the
    Postgres owner the CLI runs as, not an app user. Idempotent over the same subject.

    oidc_subject: the subject claim the provider mints this identity's tokens against.
    name: display name for a freshly minted user.
    """
    user = await admin.link_user(oidc_subject, name)
    print(user.id)


@user.command(name="list")
async def list_users() -> None:
    """List every user known to the engine, id and display name."""
    for user in await admin.list_users():
        print(f"{user.id}  {user.display_name or '-'}")


@group.command(name="add-member")
async def add_member(user: str, group: str, role: str = "editor") -> None:
    """Add a user to a group so that group's scope becomes visible to it under RLS.

    user: id of the user joining the group.
    group: name of the group the user joins.
    role: standing within the group, viewer for read-only, editor or admin to also write.
    """
    await admin.add_member(user, group, role=role)
    print(f"{user} joined {group} as {role}")


@group.command(name="remove-member")
async def remove_member(user: str, group: str) -> None:
    """Remove a user from a group, its scope no longer visible to them.

    user: id of the user leaving the group.
    group: name of the group the user leaves.
    """
    await admin.remove_member(user, group)
    print(f"{user} removed from {group}")


@group.command(name="publish")
async def publish_group(group: str, public: bool = True) -> None:
    """Publish a group so anyone can read its rows, or unpublish it back to members-only.

    group: name of the group to publish or unpublish.
    public: true to publish, false to make members-only again.
    """
    await admin.publish_group(group, public=public)
    print(f"{group} public={public}")


@group.command(name="delete")
async def delete_group(group: str) -> None:
    """Delete a group, memberships cascading and its rows falling back to their owners.

    group: name of the group to delete.
    """
    await admin.delete_group(group)
    print(f"{group} deleted")


@group.command(name="list")
async def list_groups() -> None:
    """List every group with its visibility and member count, the sharing roster."""
    for row in await admin.list_groups():
        flags = "public" if row["public"] else "members-only"
        print(f"{row['name']}  {flags}  {row['members']} members")


@data.command(name="ingest")
async def ingest(path: str, scopes: str | None = None, user: uuid.UUID | None = None) -> None:
    """Ingest a file or directory of notes and code into memory, the document count back.

    path: file or directory to ingest.
    scopes: comma-separated group names to share it with, private to the owner when null.
    user: identity that owns the stored rows, the system user when null.
    """
    count = await admin.ingest(path, scopes=scopes, user_id=user)
    print(f"ingested {count} documents from {path}")


@data.command(name="ingest-image")
async def ingest_image(
    path: str,
    caption: str | None = None,
    scopes: str | None = None,
    user: uuid.UUID | None = None,
) -> None:
    """Ingest an image into the shared multimodal space so a text query can recall it.

    path: image file to ingest.
    caption: text stored on the chunk and shown in recall, the file name when null.
    scopes: comma-separated group names to share it with, private to the owner when null.
    user: identity that owns the stored row, the system user when null.
    """
    document_id = await admin.ingest_image(path, caption=caption, scopes=scopes, user_id=user)
    print(document_id)


@graph.command(name="rebuild")
async def rebuild(
    limit: int | None = None, source: str | None = None, user: uuid.UUID | None = None
) -> None:
    """Build the graph now over the user's pending chunks, the on-demand extraction.

    limit: maximum number of chunks to process, all of them when null.
    source: restrict the build to chunks of documents whose title matches this substring.
    user: identity that owns the written claims, the system user when null.
    """
    entities, facts = await admin.rebuild(limit=limit, source=source, user_id=user)
    print(f"built {entities} entities and {facts} facts")


@graph.command(name="decay")
async def decay(half_life_days: float = 90.0, user: uuid.UUID | None = None) -> None:
    """Run the decay pass now, archiving stale facts that leave recall but stay in history.

    half_life_days: age in days at which an unaccessed fact's relevance halves.
    user: identity whose facts are decayed, the system user when null.
    """
    archived = await admin.decay(half_life_days=half_life_days, user_id=user)
    print(f"archived {archived} stale facts")


@graph.command(name="reembed")
async def reembed(user: uuid.UUID | None = None) -> None:
    """Re-embed every visible stored vector with the current embedder, a backend migration.

    user: identity whose vectors are re-embedded, the system user when null.
    """
    written = await admin.reembed(user_id=user)
    print(f"re-embedded {written} vectors")


@graph.command(name="raptor")
async def raptor(user: uuid.UUID | None = None) -> None:
    """Build the RAPTOR tree now, the recursive summary tiers above the communities.

    user: identity whose tree is built, the system user when null.
    """
    written = await admin.raptor(user_id=user)
    print(f"built {written} summaries")


@graph.command(name="forget")
async def forget(query: str, k: int = 8, user: uuid.UUID | None = None) -> None:
    """Retract the claims a query's own source notes contributed, the erasure counterpart to write.

    query: what to forget, described the way you would recall it.
    k: how many of the most relevant source notes to retract the derived claims of.
    user: identity whose notes are searched and retracted, the system user when null.
    """
    result = await admin.forget(query, k=k, user_id=user)
    print(f"retracted {result.claims} claims from {len(result.documents)} notes")
    for title in result.documents:
        print(f"  - {title}")


@data.command(name="promote")
async def promote(document: str, to_scopes: str, user: uuid.UUID | None = None) -> None:
    """Promote a document and its chunks and facts into a wider scope-set as a new audited copy.

    document: id of the source document to promote.
    to_scopes: comma-separated names of the target groups the copy is published into.
    user: identity the promotion acts under, the system user when null.
    """
    count = await admin.promote(document, to_scopes, user_id=user)
    print(f"promoted {count} rows into {to_scopes}")


@data.command(name="export")
async def export_scope(path: str, user: uuid.UUID | None = None) -> None:
    """Export a user's visible memory to a JSONL file, the scoped portable dump.

    path: the JSONL file the dump is written to.
    user: identity whose visible rows are exported, the system user when null.
    """
    report = await admin.export_scope(path, user_id=user)
    print(report.render() if hasattr(report, "render") else report)


@data.command(name="audit")
async def audit(limit: int = 20, user: uuid.UUID | None = None) -> None:
    """List the most recent visible document writes with owner, scope-set, and title.

    limit: maximum number of writes to return.
    user: identity whose visible writes are listed, the system user when null.
    """
    for doc in await admin.audit(limit=limit, user_id=user):
        scopes = ",".join(str(s) for s in doc.scopes) or "private"
        print(f"{doc.id}  {doc.kind}  [{scopes}]  {doc.title or '-'}")


@ontology.command(name="define-entity")
async def define_entity_kind(name: str, description: str, domain: str = "general") -> None:
    """Add or refine an entity type in the live ontology, refreshing the extraction snapshot.

    name: the type a content row stores, a noun in PascalCase such as Area or Milestone.
    description: one-line gloss the extraction prompt renders and the auto-create fold matches.
    domain: grouping tag, general by default, or core, coding, research, finance, personal.
    """
    await admin.define_entity_kind(name, description, domain)
    print(f"entity kind {name} defined")


@ontology.command(name="define-relation")
async def define_relation_kind(name: str, description: str, domain: str = "general") -> None:
    """Add or refine a relation predicate in the live ontology, refreshing the extraction snapshot.

    name: the predicate a fact stores, a snake_case verb phrase such as part_of or funds.
    description: one-line gloss the extraction prompt renders and the auto-create fold matches.
    domain: grouping tag, general by default, or core, coding, research, finance, personal.
    """
    await admin.define_relation_kind(name, description, domain)
    print(f"relation kind {name} defined")


@ontology.command(name="list")
async def list_ontology() -> None:
    """List every ontology kind with how much of the graph uses it, the catalog review surface."""
    for row in await admin.list_ontology():
        mark = "*" if row.structural else " "
        print(f"{mark} {row.kind:8} {row.name:24} {row.domain:9} uses={row.uses}")


@db.command(name="tasks-status")
async def tasks_status() -> None:
    """Report the autonomous engine's pending, running, failed, last-run, and lag counts."""
    status = await admin.tasks_status()
    print(status.model_dump_json(indent=2) if hasattr(status, "model_dump_json") else status)


@app.command
def profile_report() -> None:
    """Report the process-wide span timing stats mainboard.profiling collected, slowest first."""
    stats = admin.profile_report()
    for stat in stats:
        print(stat)
    if not stats:
        print("no spans recorded (set AIZK_PROFILING=1)")


@eval.command(name="bench")
async def bench(questions_file: str | None = None, k: int = 8) -> None:
    """Run the eval harness over visible memory and report hit-at-k with a per-config split.

    questions_file: a file of one question per line, or null to synthesize them from facts.
    k: how many hits and seed facts each recall surfaces.
    """
    report = await admin.bench(questions_file=questions_file, k=k)
    print(report.render() if hasattr(report, "render") else report)


@eval.command(name="sweep")
async def sweep(questions_file: str | None = None, k: int = 8, dims: str | None = None) -> None:
    """Sweep the config grid and report quality, latency, and memory for each config.

    questions_file: a file of one question per line, or null to synthesize them from facts.
    k: how many hits and seed facts each recall surfaces.
    dims: comma-separated Matryoshka widths to sweep, the live width when null.
    """
    report = await admin.sweep(questions_file=questions_file, k=k, dims=dims)
    print(report.render() if hasattr(report, "render") else report)


@eval.command(name="benchmark")
async def benchmark(name: str, dataset_path: str, k: int = 8) -> None:
    """Sweep the config grid over one external 2026 benchmark loaded from its dataset file.

    name: which benchmark to load, `evermembench` or `tempo`.
    dataset_path: path to the benchmark's JSONL file.
    k: how many hits and seed facts each recall surfaces.
    """
    report = await admin.benchmark(name, dataset_path, k=k)
    print(report.render() if hasattr(report, "render") else report)


@db.command(name="setup")
async def setup() -> None:
    """Bring the database to a ready state, migrating to head and installing the queue schema."""
    report = await admin.setup()
    print(f"migrated {report.migrated_from} -> {report.migrated_to}")


@db.command(name="health")
async def health() -> None:
    """Report the engine's schema, row security, row-count, queue, and serving-endpoint state."""
    report = await admin.health()
    print(report.model_dump_json(indent=2) if hasattr(report, "model_dump_json") else report)


if __name__ == "__main__":
    app()
