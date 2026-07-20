import asyncio
import json
import sys
from collections.abc import Awaitable
from pathlib import Path
from typing import TYPE_CHECKING, cast

import uvicorn
from cyclopts import App
from loguru import logger
from mainboard.profiling import Profiler
from pydantic import UUID5, UUID7, JsonValue, TypeAdapter

from alembic import command

from .. import admin, ops
from .. import backup as backup_ops
from ..api.app import AizkAPI
from ..artifacts.uploads import InertIntake, UploadBox
from ..auth import Auth
from ..background.jobs.conversion import retry_failed_artifacts
from ..background.jobs.maintenance import retry_failed_profile_projections
from ..background.jobs.projection import retry_failed_chunks
from ..background.queue import install_queue_schema
from ..background.schedule import run_worker
from ..config import Settings, settings
from ..integrations.logto import LogtoClient, LogtoPolicy
from ..mcp.server import AizkMCP
from ..runtime import Runtime
from ..store import Relation
from ..store.models.tables import RelationPolicy
from ..usage import observe

if TYPE_CHECKING:
    from ..artifacts.service import ArtifactIntake

_JSON_FIELDS = TypeAdapter(dict[str, JsonValue])
_SENSITIVE_FIELDS = frozenset(
    {
        "admin_database_url",
        "backup_database_url",
        "database_url",
    }
)
_SENSITIVE_SUFFIXES = ("_api_key", "_password", "_secret", "_token")

admin_app = App(name="admin", help="Operate and inspect an AIZK server.")
server_app = App(name="server", help="Run the MCP, API, and worker processes.")
queue_app = App(name="queue", help="Inspect and recover background work.")
retry_app = App(name="retry", help="Requeue repaired conversion, graph, or profile work.")
database_app = App(name="database", help="Provision, migrate, back up, and restore storage.")
graph_app = App(name="graph", help="Maintain and inspect the knowledge graph.")
data_app = App(name="data", help="Ingest, promote, export, and audit memory.")
ontology_app = App(name="ontology", help="Inspect and maintain the controlled vocabulary.")
auth_app = App(name="auth", help="Validate and reconcile server authorization policy.")
settings_app = App(name="settings", help="Inspect and validate effective server settings.")
api_app = App(name="api", help="Maintain browser API development artifacts.")

queue_app.command(retry_app)
for subcommand in (
    server_app,
    queue_app,
    database_app,
    graph_app,
    data_app,
    ontology_app,
    auth_app,
    settings_app,
    api_app,
):
    admin_app.command(subcommand)


async def _run_profiled[Result](operation: Awaitable[Result]) -> Result:
    """Run one operation with optional low-impact process profiling."""
    if not settings.profiling:
        return await operation
    profiler = Profiler(features=Profiler.Feature.SPANS | Profiler.Feature.DEVICE)
    try:
        with profiler:
            return await operation
    finally:
        logger.info("{}", profiler.report())


def _json(payload: dict[str, JsonValue]) -> str:
    """Serialize one command result with stable field ordering."""
    return json.dumps(payload, indent=2, sort_keys=True)


def _settings_payload(name: str | None = None) -> dict[str, JsonValue]:
    """Return effective settings with every credential-bearing field masked."""
    payload = _JSON_FIELDS.validate_python(settings.model_dump(mode="json"))
    for field in payload:
        if field in _SENSITIVE_FIELDS or field.endswith(_SENSITIVE_SUFFIXES):
            payload[field] = "<redacted>"
    if name is None:
        return payload
    name = name.replace("-", "_")
    try:
        return {name: payload[name]}
    except KeyError as error:
        raise ValueError(f"unknown setting {name!r}") from error


@admin_app.command(name="health")
async def health() -> None:
    """Report schema, security, queues, models, and serving endpoint health."""
    report = await admin.health()
    print(report.model_dump_json(indent=2))


@server_app.command(name="mcp")
async def serve_mcp() -> None:
    """Run the HTTP MCP server and its optional colocated worker."""
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
        if settings.serve_with_worker:
            await _run_profiled(asyncio.gather(serving, run_worker(runtime)))
        else:
            await _run_profiled(serving)


@server_app.command(name="api")
async def serve_api() -> None:
    """Run the browser JSON API service."""
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
        await _run_profiled(server.serve())


@server_app.command(name="worker")
async def serve_worker(batch_size: int | None = None) -> None:
    """Run the autonomous queue worker and scheduler until interrupted."""
    async with Runtime.assemble(settings) as runtime:
        observe(runtime.database)
        await _run_profiled(run_worker(runtime, batch_size=batch_size))


@queue_app.command(name="status")
async def queue_status() -> None:
    """Report pending, running, failed, last-run, and lag counts."""
    report = await admin.tasks_status()
    print(report.model_dump_json(indent=2))


@queue_app.command(name="doctor")
async def doctor(
    stale_minutes: int = 15,
    long_running_minutes: int = 60,
    history_hours: int = 24,
    limit: int = 50,
    show_error_messages: bool = False,
) -> None:
    """Diagnose queue failures, unhealthy leases, and artifact conversions."""
    report = await ops.doctor(
        stale_minutes=stale_minutes,
        long_running_minutes=long_running_minutes,
        history_hours=history_hours,
        limit=limit,
        show_error_messages=show_error_messages,
    )
    print(_json(_JSON_FIELDS.validate_python(report.model_dump(mode="json"))))
    if not report.healthy:
        raise SystemExit(1)


@retry_app.command(name="conversion")
async def retry_conversion(limit: int = 100) -> None:
    """Recover retained queue failures and durable conversion failures."""
    count = await retry_failed_artifacts(limit)
    print(f"recovered {count} failed conversions")


@retry_app.command(name="graph")
async def retry_graph(limit: int = 100) -> None:
    """Requeue retained graph projection failures after a repair."""
    count = await retry_failed_chunks(limit)
    print(f"requeued {count} failed graph jobs")


@retry_app.command(name="profile")
async def retry_profile(limit: int = 100) -> None:
    """Requeue retained profile projection failures after a repair."""
    count = await retry_failed_profile_projections(limit)
    print(f"requeued {count} failed profile jobs")


@database_app.command(name="setup")
async def setup_database() -> None:
    """Migrate storage to head and install the queue schema."""
    report = await admin.setup()
    print(f"migrated {report.migrated_from} -> {report.migrated_to}")


@database_app.command(name="migrate")
def migrate_database(sql: bool = False) -> None:
    """Apply migrations or write their offline PostgreSQL script."""
    ops.run_alembic(command.upgrade, ops.alembic_config(), "head", sql=sql)
    if not sql:
        print("done")


@database_app.command(name="make-migration")
def make_migration(message: str) -> None:
    """Autogenerate a database migration from current model metadata."""
    ops.run_alembic(command.revision, ops.alembic_config(), message=message, autogenerate=True)
    print("done")


@database_app.command(name="install-queue")
async def install_queue() -> None:
    """Install the PgQueuer schema and grant application role access."""
    await install_queue_schema()
    print("done")


@database_app.command(name="check-rls")
async def check_rls() -> None:
    """Verify scoped tables force the canonical row security policies."""
    violations = await ops.scoped_rls_violations()
    if violations:
        for reason in violations:
            print(reason)
        sys.exit(1)
    print("ok")


@database_app.command(name="backup")
async def backup_database(path: str) -> None:
    """Dump the complete database to one portable archive."""
    report = await backup_ops.backup_database(path)
    print(f"backed up {report.bytes} bytes to {report.path}")


@database_app.command(name="restore")
async def restore_database(path: str) -> None:
    """Restore one archive into the configured database."""
    report = await backup_ops.restore_database(path)
    print(f"restored {report.path} into {report.database}")


@database_app.command(name="reset")
async def reset_database(confirm: str) -> None:
    """Erase only the configured database after exact-name confirmation."""
    if confirm != settings.db_name:
        raise ValueError(f"confirmation must exactly match {settings.db_name!r}")
    report = await admin.reset_database()
    print(f"reset {report.database} at {report.migrated_to}")


@graph_app.command(name="rebuild")
async def rebuild_graph(
    limit: int | None = None,
    source: str | None = None,
    user: UUID5 | None = None,
) -> None:
    """Build graph projections over pending chunks."""
    async with Runtime.assemble(settings) as runtime:
        entities, facts = await _run_profiled(
            admin.rebuild(runtime.graph, limit=limit, source=source, user_id=user)
        )
    print(f"built {entities} entities and {facts} facts")


@graph_app.command(name="diagnose-extraction")
async def diagnose_extraction(chunk: UUID7) -> None:
    """Explain extraction and grounding for one chunk without writing."""
    async with Runtime.assemble(settings) as runtime:
        report = await admin.diagnose_extraction(runtime.extractor, chunk)
    print(report.model_dump_json(indent=2))


@graph_app.command(name="decay")
async def decay_graph(half_life_days: float = 90.0, user: UUID5 | None = None) -> None:
    """Archive stale facts that fall below the active-memory floor."""
    archived = await _run_profiled(admin.decay(half_life_days=half_life_days, user_id=user))
    print(f"archived {archived} stale facts")


@graph_app.command(name="reembed")
async def reembed_graph(user: UUID5 | None = None) -> None:
    """Re-embed visible vectors with the configured embedder."""
    written = await _run_profiled(admin.reembed(user_id=user))
    print(f"re-embedded {written} vectors")


@graph_app.command(name="communities")
async def build_communities(user: UUID5 | None = None) -> None:
    """Build graph communities and their global summaries."""
    written = await _run_profiled(admin.communities(user_id=user))
    print(f"built {written} communities")


@graph_app.command(name="raptor")
async def build_raptor(user: UUID5 | None = None) -> None:
    """Build recursive summary tiers above graph communities."""
    async with Runtime.assemble(settings) as runtime:
        written = await _run_profiled(admin.raptor(runtime.llm, runtime.embed, user_id=user))
    print(f"built {written} summaries")


@graph_app.command(name="forget")
async def forget_graph(query: str, k: int = 8, user: UUID5 | None = None) -> None:
    """Retract claims contributed by the source notes matching one query."""
    result = await _run_profiled(admin.forget(query, k=k, user_id=user))
    print(f"retracted {result.claims} claims from {len(result.documents)} notes")
    for title in result.documents:
        print(f"  - {title}")


@data_app.command(name="ingest")
async def ingest_data(path: str, scopes: str | None = None, user: UUID5 | None = None) -> None:
    """Ingest a file or directory through the local operator boundary."""
    count = await admin.ingest(path, scopes=scopes, user_id=user)
    print(f"ingested {count} documents from {path}")


@data_app.command(name="promote")
async def promote_data(document: str, to_scopes: str, user: UUID5 | None = None) -> None:
    """Copy one document and its graph into wider scopes."""
    count = await admin.promote(document, to_scopes, user_id=user)
    print(f"promoted {count} document into {to_scopes}")


@data_app.command(name="export")
async def export_data(path: str, user: UUID5 | None = None) -> None:
    """Export visible memory to a scoped JSONL file."""
    report = await admin.export_scope(path, user_id=user)
    print(report.render())


@data_app.command(name="audit")
async def audit_data(limit: int = 20, user: UUID5 | None = None) -> None:
    """List the most recent visible document writes."""
    for document in await admin.audit(limit=limit, user_id=user):
        scopes = ",".join(str(scope) for scope in document.scopes) or "private"
        print(
            f"{document.id}  {document.subject_type or 'source'}  "
            f"[{scopes}]  {document.title or '-'}"
        )


@ontology_app.command(name="define-entity")
async def define_entity(name: str, description: str, domain: str = "general") -> None:
    """Add or refine one entity kind."""
    await admin.define_entity_kind(name, description, domain)
    print(f"entity kind {name} defined")


@ontology_app.command(name="define-relation")
async def define_relation(
    name: str,
    description: str,
    domain: str = "general",
    policy: RelationPolicy = Relation.Policy.set,
) -> None:
    """Add or refine one relation kind."""
    await admin.define_relation_kind(name, description, domain, policy)
    print(f"relation kind {name} defined")


@ontology_app.command(name="list")
async def list_ontology() -> None:
    """List ontology kinds and their live graph usage."""
    for row in await admin.list_ontology():
        mark = "*" if row.structural else " "
        print(f"{mark} {row.kind:8} {row.name:24} {row.domain:9} uses={row.uses}")


@auth_app.command(name="audit")
async def audit_auth() -> None:
    """Report drift between Logto and configured authorization policy."""
    client = LogtoClient(settings)
    try:
        report = await LogtoPolicy(client).audit()
    finally:
        await client.close()
    print(report.model_dump_json(indent=2))
    if not report.clean:
        raise SystemExit(1)


@auth_app.command(name="apply")
async def apply_auth() -> None:
    """Reconcile Logto with configured authorization policy."""
    client = LogtoClient(settings)
    try:
        report = await LogtoPolicy(client).apply()
    finally:
        await client.close()
    print(report.model_dump_json(indent=2))


@auth_app.command(name="check-public")
def check_public_auth() -> None:
    """Confirm that public MCP authentication is fully configured."""
    print("public authentication configuration is complete")


@auth_app.command(name="check-web")
def check_web_auth() -> None:
    """Confirm that browser authentication is fully configured."""
    if settings.web_public_url is None:
        raise RuntimeError("web deployment requires web_public_url")
    print(f"web authentication is complete at {settings.web_callback_url}")


@settings_app.command(name="show")
def show_settings(name: str | None = None) -> None:
    """Print effective settings with secrets redacted."""
    print(_json(_settings_payload(name)))


@settings_app.command(name="validate")
def validate_settings() -> None:
    """Validate effective environment settings without starting services."""
    Settings()
    print(_json({"valid": True}))


@api_app.command(name="openapi")
def write_openapi(path: Path = Path("src/web/openapi.json")) -> None:
    """Write the browser API schema for generated clients."""
    inert = InertIntake()
    service = AizkAPI(Auth(), UploadBox(intake=inert), cast("ArtifactIntake", inert))
    path.write_text(json.dumps(service.app().openapi(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")
