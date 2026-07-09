from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import asyncpg
import httpx
from loguru import logger
from patos import FrozenModel
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from .background.queue import install_queue_schema
from .background.status import TasksStatus, tasks_overview
from .config import settings
from .extract import ontology
from .store import TableBase, as_system, verify_scoped_rls

# the tables a health read counts, the main entities of the store rather than every join and
# mixin table, read through the owner's superuser connection so the count is the true total
# rather than one user's own row-level-security-narrowed slice.
MAIN_TABLES = (
    "document",
    "chunk",
    "entity_content",
    "entity_claim",
    "fact_content",
    "fact_claim",
    "community",
    "profile",
    "session_item",
)

# the OpenAI-compatible endpoints a health read probes for reachability, named for the lane each
# serves.
SERVING_ENDPOINTS = (
    ("embed", settings.embed_url),
    ("rerank", settings.rerank_url),
    ("llm", settings.llm_url),
)

# wall-clock ceiling on one endpoint reachability probe, short since a health read waits on as
# many of these as are configured and a hung endpoint should not hang the whole report.
PROBE_TIMEOUT = 2.0


class SetupReport(FrozenModel):
    """What `setup` found already current versus what it applied, the idempotent bootstrap read.

    migrated_from: alembic revision the database was on before this run, equal to migrated_to
        when it was already at head.
    migrated_to: alembic head revision the database sits at once this run returns.
    queue_installed: whether this run created the pgqueuer schema, false when it already existed.
    """

    migrated_from: str | None
    migrated_to: str
    queue_installed: bool


class SchemaHealth(FrozenModel):
    """Alembic migration state, the schema half of a health read.

    current: revision the database is stamped at, null on a fresh unmigrated database.
    head: revision the installed package's own migrations consider current.
    up_to_date: whether current already equals head.
    """

    current: str | None
    head: str
    up_to_date: bool


class EndpointHealth(FrozenModel):
    """Reachability of one OpenAI-compatible serving endpoint, the model half of a health read.

    name: which lane this endpoint serves, embed, rerank, or llm.
    url: base URL probed.
    reachable: whether a short GET against its models path answered without a network error.
    """

    name: str
    url: str
    reachable: bool


class HealthReport(FrozenModel):
    """The engine's operational snapshot, schema, row security, row counts, queue, and endpoints.

    migration: alembic migration state.
    rls_violations: reasons a scoped table fails the no-leak contract, empty when clean.
    row_counts: live row count per main table, read past row level security.
    queue: the autonomous engine's pending, running, failed, and lag snapshot.
    endpoints: reachability of the embed, rerank, and llm serving endpoints.
    """

    migration: SchemaHealth
    rls_violations: list[str]
    row_counts: dict[str, int]
    queue: TasksStatus
    endpoints: list[EndpointHealth]


def alembic_config() -> Config:
    """Build the alembic Config pointed at the migration scripts shipped inside the package."""
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "store" / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.admin_database_url)
    return config


def run_alembic[T](fn: Callable[..., T], *args: object, **kwargs: object) -> T:
    """Run one blocking alembic `command` call on a dedicated worker thread, returning its result.

    alembic's command API is synchronous, but `store/migrations/env.py` opens its DSN through an
    async engine and drives it with its own top-level `asyncio.run`, which raises when the calling
    thread already runs a loop, exactly the case a caller already inside an event loop hits. A
    private thread carries no loop of its own, so the alembic call is safe to make from a plain
    synchronous caller and from inside an already-running event loop alike, blocking either way
    until the migration finishes.

    fn: the alembic `command` callable to run, `command.upgrade` or `command.revision`.
    args: positional arguments forwarded to `fn`.
    kwargs: keyword arguments forwarded to `fn`.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args, **kwargs).result()


def alembic_head(config: Config) -> str:
    """The revision the installed package's own migration scripts consider current.

    config: the alembic Config the scripts are read from.
    """
    return ScriptDirectory.from_config(config).get_current_head() or "head"


async def alembic_current() -> str | None:
    """The revision the live database is stamped at, null on a fresh unmigrated database.

    Opens its own short-lived admin connection, the owner-role superuser every migration and
    catalog read already runs as, and bridges to alembic's synchronous `MigrationContext` through
    `AsyncConnection.run_sync` rather than opening a second, blocking engine.
    """
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.connect() as connection:
            return await connection.run_sync(
                lambda sync: MigrationContext.configure(sync).get_current_revision()
            )
    finally:
        await admin.dispose()


async def queue_schema_present() -> bool:
    """Whether the pgqueuer tables already exist, setup's own idempotency probe."""
    connection = await asyncpg.connect(settings.asyncpg_dsn)
    try:
        return bool(await connection.fetchval("SELECT to_regclass('pgqueuer') IS NOT NULL"))
    finally:
        await connection.close()


async def scoped_rls_violations() -> list[str]:
    """Reasons the live schema fails the no-leak contract for any registered scoped table.

    Reads the catalog through the owning role, the only role that can see every table's row
    security flags and policy expressions, checking each Scoped model against `verify_scoped_rls`.
    """
    expected = set(TableBase.metadata.info["rls"])
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.connect() as connection:
            return await connection.run_sync(lambda sync: verify_scoped_rls(sync, expected))
    finally:
        await admin.dispose()


async def row_counts() -> dict[str, int]:
    """Live row count per `MAIN_TABLES` entry, read through the owner's superuser connection.

    The owner role is the actual Postgres superuser this stack provisions (`initdb/roles.sh`), so
    it bypasses row level security entirely and this count is the true total rather than one
    user's own narrowed slice.
    """
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.connect() as connection:
            return {
                table: (
                    await connection.execute(text(f"SELECT count(*) FROM {table}"))
                ).scalar_one()
                for table in MAIN_TABLES
            }
    finally:
        await admin.dispose()


async def probe_endpoint(name: str, url: str) -> EndpointHealth:
    """Probe one OpenAI-compatible endpoint's models path with a short timeout.

    name: which lane this endpoint serves, embed, rerank, or llm.
    url: base URL to probe, ending at the /v1 prefix.
    """
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            response = await client.get(f"{url}/models")
        reachable = response.status_code < 500
    except httpx.HTTPError:
        reachable = False
    return EndpointHealth(name=name, url=url, reachable=reachable)


async def grant_app_role_privileges() -> None:
    """Grant the app role CRUD on the public schema, mirroring `initdb/roles.sh` on any database.

    `roles.sh` only ever runs once, against the original database's fresh volume at container
    init, so a later database on the same Postgres instance (a scratch database for a bounded
    test, say) never receives its schema USAGE, per-table CRUD, or default-privilege grants. A
    fresh database migrated to head still has every Scoped table's own `apply_scoped_rls` grant
    (0001_init's per-table belt), but an unscoped table such as `group_`, `membership`, or
    `user` carries none. This closes that gap so `setup` alone, with no manual psql grant,
    makes any database ready. Every statement is a plain idempotent GRANT, safe to rerun.
    """
    role = settings.app_role
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.begin() as connection:
            for statement in (
                f"GRANT USAGE ON SCHEMA public TO {role}",
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, "
                f"DELETE ON TABLES TO {role}",
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES "
                f"TO {role}",
            ):
                await connection.execute(text(statement))
    finally:
        await admin.dispose()


async def enable_query_stats() -> None:
    """Create pg_stat_statements, tolerating a Postgres not yet restarted with the library loaded.

    `CREATE EXTENSION` itself creates the catalog objects unconditionally, whether or not
    `shared_preload_libraries` names the module yet, but the view stays unqueryable ("pg_stat_
    statements must be loaded via shared_preload_libraries") until Postgres restarts with the
    updated `command` (`docker-compose.yml`'s own comment on the `db` service). This call stays
    idempotent and safe either side of that restart. The except below is a second line of
    defense in case a future extension version raises at create time instead, so `setup` never
    breaks on either behavior.
    """
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_stat_statements"))
    except DBAPIError as error:
        if "shared_preload_libraries" not in str(error):
            raise
        logger.warning(
            "pg_stat_statements not yet loaded, restart Postgres with the updated "
            "shared_preload_libraries to activate it: {}",
            error,
        )
    finally:
        await admin.dispose()


async def setup() -> SetupReport:
    """Bring the database to a ready state, migrate to head, install the queue schema, grant CRUD.

    Idempotent, safe to run against a fresh database or an already-current one, alembic no-ops at
    head, `install_queue_schema` tolerates an already-installed schema,
    `grant_app_role_privileges` is a plain idempotent GRANT set, and `enable_query_stats`
    tolerates a not-yet-restarted Postgres, so this is the one bootstrap call the MCP `setup`
    tool, the startup auto-setup, and the CLI's `migrate` and `install-queue` commands all run,
    and the only call any database on the instance needs to become ready. Refreshing the ontology
    cache is the final step, since every gate check and extraction call downstream of a fresh
    migration reads the live catalog through it, never a class body fixed at import time.
    """
    before = await alembic_current()
    already_queued = await queue_schema_present()
    config = alembic_config()
    run_alembic(command.upgrade, config, "head")
    if not already_queued:
        await install_queue_schema()
    await grant_app_role_privileges()
    await enable_query_stats()
    async with as_system():
        await ontology.refresh()
    return SetupReport(
        migrated_from=before, migrated_to=alembic_head(config), queue_installed=not already_queued
    )


async def health() -> HealthReport:
    """Read the engine's schema, row security, row-count, queue, and serving-endpoint state.

    Every section reads independently and none depends on another's result, so a caller wanting
    only the schema half still pays the full read. `setup`'s own health probe reuses this rather
    than a narrower schema-only check, since a startup diagnostic is cheap next to serving traffic.
    """
    current = await alembic_current()
    head = alembic_head(alembic_config())
    endpoints = [await probe_endpoint(name, url) for name, url in SERVING_ENDPOINTS]
    return HealthReport(
        migration=SchemaHealth(current=current, head=head, up_to_date=current == head),
        rls_violations=await scoped_rls_violations(),
        row_counts=await row_counts(),
        queue=await tasks_overview(),
        endpoints=endpoints,
    )
