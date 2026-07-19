import asyncio
import json
import os
import sys
from collections.abc import Awaitable
from pathlib import Path
from typing import TYPE_CHECKING, cast

import uvicorn
from cyclopts import App
from loguru import logger
from mainboard.profiling import Profiler
from pydantic import UUID5, UUID7

from alembic import command

from . import admin, ops
from . import backup as backup_ops
from .api.app import AizkAPI
from .artifacts.uploads import InertIntake, UploadBox
from .auth import Auth
from .background.jobs.projection import enqueue_pending, retry_failed_chunks
from .background.queue import install_queue_schema
from .background.schedule import run_worker
from .config import settings
from .extract.ingest import ingest_text
from .integrations.logto import LogtoClient, LogtoPolicy
from .mcp.server import AizkMCP
from .retrieval import RecallResult, recall
from .runtime import Runtime
from .store import Relation
from .store.identity import User
from .store.models.tables import RelationPolicy
from .usage import observe, sink

if TYPE_CHECKING:
    from .artifacts.service import ArtifactIntake

# Stop hooks provide the active transcript through this variable.
_TRANSCRIPT_ENV = "AIZK_SESSION_TRANSCRIPT"

# Default context loaded at session start
_PROJECT_CONTEXT_QUERY = "recent decisions, patterns, gotchas, and project context"

app = App(
    name="aizk",
    help="Process and bootstrap entrypoint for the aizk memory engine, whose verbs are MCP tools.",
)

# Operator verbs are grouped by noun while hooks and serving remain top-level.
graph = App(name="graph", help="Graph maintenance: rebuild, decay, reembed, raptor, forget.")
ontology = App(name="ontology", help="Ontology: the entity types and relation predicates.")
data = App(name="data", help="Data: ingest, export, audit, and promote documents.")
db = App(name="db", help="Database and engine ops: setup, health, migrations, backup, restore.")
logto = App(name="logto", help="Logto authorization policy audit and reconciliation.")
for _sub in (graph, ontology, data, db, logto):
    app.command(_sub)


async def _run_profiled[Result](operation: Awaitable[Result]) -> Result:
    """Run one process lifetime with low-impact spans and target-process GPU telemetry."""
    if not settings.profiling:
        return await operation
    profiler = Profiler(features=Profiler.Feature.SPANS | Profiler.Feature.DEVICE)
    try:
        with profiler:
            return await operation
    finally:
        logger.info("{}", profiler.report())


@db.command(name="migrate")
def migrate(sql: bool = False) -> None:
    """Apply migrations or write their offline PostgreSQL script with `--sql`."""
    ops.run_alembic(command.upgrade, ops.alembic_config(), "head", sql=sql)
    if not sql:
        print("done")


@db.command(name="makemigrations")
def makemigrations(message: str) -> None:
    """Autogenerate a new database migration from the current model metadata."""
    ops.run_alembic(command.revision, ops.alembic_config(), message=message, autogenerate=True)
    print("done")


@db.command(name="check-rls")
async def check_rls() -> None:
    """Verify every scoped table forces row level security with the canonical scope policies."""
    violations = await ops.scoped_rls_violations()
    if violations:
        for reason in violations:
            print(reason)
        sys.exit(1)
    print("ok")


@app.command
async def worker(batch_size: int | None = None) -> None:
    """Run the autonomous engine, the queue and the scheduler together, until interrupted."""
    async with Runtime.assemble(settings) as runtime:
        observe(runtime.database)
        try:
            await _run_profiled(run_worker(runtime, batch_size=batch_size))
        finally:
            await sink.drain()


@db.command(name="install-queue")
async def install_queue() -> None:
    """Install the pgqueuer schema as the owner and grant the app role access."""
    await install_queue_schema()
    print("done")


@db.command(name="retry-failed-chunks")
async def retry_failed_chunk_jobs(limit: int = 100) -> None:
    """Requeue retained chunk projection failures after deploying a repair."""
    count = await retry_failed_chunks(limit)
    print(f"requeued {count} failed chunk jobs")


@db.command(name="backup")
async def backup(path: str) -> None:
    """Dump the whole database to a portable archive at `path`, the durable snapshot of
    memory."""
    report = await backup_ops.backup_database(path)
    print(f"backed up {report.bytes} bytes to {report.path}")


@db.command(name="restore")
async def restore(path: str) -> None:
    """Load a backup archive back into the configured database, overwriting its current
    contents."""
    report = await backup_ops.restore_database(path)
    print(f"restored {report.path} into {report.database}")


@db.command(name="reset")
async def reset_database(confirm: str) -> None:
    """Erase only the Aizk database after its exact name is provided as confirmation."""
    if confirm != settings.db_name:
        raise ValueError(f"confirmation must exactly match {settings.db_name!r}")
    report = await admin.reset_database()
    print(f"reset {report.database} at {report.migrated_to}")


@app.command
async def serve_mcp() -> None:
    """Run the HTTP MCP server and optionally its worker in the same local process."""
    if settings.auto_setup:
        applied = await ops.setup()
        logger.info("database ready at {}", applied.migrated_to)
    logger.info("serving aizk mcp over HTTP, worker={}", settings.serve_with_worker)
    async with Runtime.assemble(settings) as runtime:
        observe(runtime.database)
        server = AizkMCP(
            runtime.auth,
            runtime.store,
            runtime.uploads,
            runtime.artifacts.intake,
            runtime.settings,
        )
        serving = server.run_http_async(host=settings.mcp_host, port=settings.mcp_port)
        try:
            if settings.serve_with_worker:
                await _run_profiled(asyncio.gather(serving, run_worker(runtime)))
            else:
                await _run_profiled(serving)
        finally:
            await sink.drain()


@app.command
async def serve_api() -> None:
    """Run the browser JSON API service over HTTP, mirroring the MCP serving command."""
    if settings.auto_setup:
        applied = await ops.setup()
        logger.info("database ready at {}", applied.migrated_to)
    logger.info("serving aizk api over HTTP at {}:{}", settings.api_host, settings.api_port)
    async with Runtime.assemble(settings) as runtime:
        observe(runtime.database)
        service = AizkAPI(runtime.auth, runtime.uploads, runtime.artifacts.intake)
        server = uvicorn.Server(
            uvicorn.Config(service.app(), host=settings.api_host, port=settings.api_port)
        )
        try:
            await _run_profiled(server.serve())
        finally:
            await sink.drain()


@app.command(name="openapi")
def openapi(path: Path = Path("src/web/openapi.json")) -> None:
    """Write the browser API's OpenAPI schema where the web client generator reads it."""
    inert = InertIntake()
    service = AizkAPI(Auth(), UploadBox(intake=inert), cast("ArtifactIntake", inert))
    path.write_text(json.dumps(service.app().openapi(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")


@app.command(name="check-public")
def check_public() -> None:
    """Confirm that public authentication is complete before the MCP server starts.

    The command succeeds only after `Settings.complete_auth` has validated the public
    URL, Logto issuer, Logto Management API client, and OAuth web application.
    """
    print("public authentication configuration is complete")


@app.command(name="check-web")
def check_web() -> None:
    """Confirm that the browser UI and its confidential Logto application are configured."""
    if settings.web_public_url is None:
        raise RuntimeError("web deployment requires web_public_url")
    print(f"web authentication is complete at {settings.web_callback_url}")


@logto.command(name="audit")
async def audit_logto() -> None:
    """Report drift between Logto and the policy configured through `AIZK_` settings."""
    client = LogtoClient(settings)
    try:
        report = await LogtoPolicy(client).audit()
    finally:
        await client.close()
    print(report.model_dump_json(indent=2))
    if not report.clean:
        raise SystemExit(1)


@logto.command(name="apply")
async def apply_logto() -> None:
    """Reconcile Logto with the configured AIZK authorization policy and print each mutation."""
    client = LogtoClient(settings)
    try:
        report = await LogtoPolicy(client).apply()
    finally:
        await client.close()
    print(report.model_dump_json(indent=2))


@app.command
async def recall_context(
    query: str | None = None,
    k: int = 8,
    user: UUID5 | None = None,
) -> None:
    """Recall memory and print it for a SessionStart hook to inject as context."""
    candidates = await recall(
        query or _PROJECT_CONTEXT_QUERY,
        user=User.system((user or settings.system_user_id,)),
        k=k,
    )
    print(await RecallResult.from_candidates(candidates).to_markdown() or "no context recalled")


@app.command
async def capture_session(
    user: UUID5 | None = None,
) -> None:
    """Capture the session's decisions into memory for a Stop hook to run at the end of a
    session."""
    transcript = os.environ.get(_TRANSCRIPT_ENV)
    if not transcript or not Path(transcript).is_file():
        print("no session transcript to capture")
        return
    user_id = user or settings.system_user_id
    text_content = Path(transcript).read_text(encoding="utf-8")

    document_id = await ingest_text(
        User.system((user_id,)),
        text_content,
        title=Path(transcript).stem,
        created_by=user_id,
        scopes=frozenset({user_id}),
    )
    await enqueue_pending(scopes=frozenset({user_id}))
    print(f"captured session into document {document_id}")


@data.command(name="ingest")
async def ingest(path: str, scopes: str | None = None, user: UUID5 | None = None) -> None:
    """Ingest a file or directory of notes and code into memory, the document count back."""
    count = await admin.ingest(path, scopes=scopes, user_id=user)
    print(f"ingested {count} documents from {path}")


@graph.command(name="rebuild")
async def rebuild(
    limit: int | None = None, source: str | None = None, user: UUID5 | None = None
) -> None:
    """Build the graph now over the user's pending chunks, the on-demand extraction."""
    async with Runtime.assemble(settings) as runtime:
        entities, facts = await _run_profiled(
            admin.rebuild(runtime.graph, limit=limit, source=source, user_id=user)
        )
    print(f"built {entities} entities and {facts} facts")


@graph.command(name="diagnose-extraction")
async def diagnose_extraction(chunk: UUID7) -> None:
    """Explain extraction and exact grounding for one stored chunk without writing."""
    async with Runtime.assemble(settings) as runtime:
        report = await admin.diagnose_extraction(runtime.extractor, chunk)
    print(report.model_dump_json(indent=2))


@graph.command(name="decay")
async def decay(half_life_days: float = 90.0, user: UUID5 | None = None) -> None:
    """Run the decay pass now, archiving stale facts that leave recall but stay in history."""
    archived = await _run_profiled(admin.decay(half_life_days=half_life_days, user_id=user))
    print(f"archived {archived} stale facts")


@graph.command(name="reembed")
async def reembed(user: UUID5 | None = None) -> None:
    """Re-embed every visible stored vector with the current embedder, a backend migration."""
    written = await _run_profiled(admin.reembed(user_id=user))
    print(f"re-embedded {written} vectors")


@graph.command(name="raptor")
async def raptor(user: UUID5 | None = None) -> None:
    """Build the RAPTOR tree now, the recursive summary tiers above the communities."""
    async with Runtime.assemble(settings) as runtime:
        written = await _run_profiled(admin.raptor(runtime.llm, runtime.embed, user_id=user))
    print(f"built {written} summaries")


@graph.command(name="communities")
async def communities(user: UUID5 | None = None) -> None:
    """Build graph communities and their global summaries now."""
    written = await _run_profiled(admin.communities(user_id=user))
    print(f"built {written} communities")


@graph.command(name="forget")
async def forget(query: str, k: int = 8, user: UUID5 | None = None) -> None:
    """Retract the claims a query's own source notes contributed, the erasure counterpart to
    write."""
    result = await _run_profiled(admin.forget(query, k=k, user_id=user))
    print(f"retracted {result.claims} claims from {len(result.documents)} notes")
    for title in result.documents:
        print(f"  - {title}")


@data.command(name="promote")
async def promote(document: str, to_scopes: str, user: UUID5 | None = None) -> None:
    """Promote a document and its chunks and facts into a wider scope-set as a new audited
    copy."""
    count = await admin.promote(document, to_scopes, user_id=user)
    print(f"promoted {count} document into {to_scopes}")


@data.command(name="export")
async def export_scope(path: str, user: UUID5 | None = None) -> None:
    """Export a user's visible memory to a JSONL file, the scoped portable dump."""
    report = await admin.export_scope(path, user_id=user)
    print(report.render())


@data.command(name="audit")
async def audit(limit: int = 20, user: UUID5 | None = None) -> None:
    """List the most recent visible document writes with creator, scope set, and title."""
    for doc in await admin.audit(limit=limit, user_id=user):
        scopes = ",".join(str(s) for s in doc.scopes) or "private"
        print(f"{doc.id}  {doc.subject_type or 'source'}  [{scopes}]  {doc.title or '-'}")


@ontology.command(name="define-entity")
async def define_entity_kind(name: str, description: str, domain: str = "general") -> None:
    """Add or refine an entity type in the live ontology and refresh its prompt."""
    await admin.define_entity_kind(name, description, domain)
    print(f"entity kind {name} defined")


@ontology.command(name="define-relation")
async def define_relation_kind(
    name: str,
    description: str,
    domain: str = "general",
    policy: RelationPolicy = Relation.Policy.set,
) -> None:
    """Add or refine a relation predicate in the live ontology and refresh its prompt."""
    await admin.define_relation_kind(name, description, domain, policy)
    print(f"relation kind {name} defined")


@ontology.command(name="list")
async def list_ontology() -> None:
    """List every ontology kind with how much of the graph uses it, the catalog inspection
    surface."""
    for row in await admin.list_ontology():
        mark = "*" if row.structural else " "
        print(f"{mark} {row.kind:8} {row.name:24} {row.domain:9} uses={row.uses}")


@db.command(name="tasks-status")
async def tasks_status() -> None:
    """Report the autonomous engine's pending, running, failed, last-run, and lag counts."""
    status = await admin.tasks_status()
    print(status.model_dump_json(indent=2))


@db.command(name="setup")
async def setup() -> None:
    """Bring the database to a ready state, migrating to head and installing the queue
    schema."""
    report = await admin.setup()
    print(f"migrated {report.migrated_from} -> {report.migrated_to}")


@db.command(name="health")
async def health() -> None:
    """Report the engine's schema, row security, row-count, queue, and serving-endpoint
    state."""
    report = await admin.health()
    print(report.model_dump_json(indent=2) if hasattr(report, "model_dump_json") else report)
