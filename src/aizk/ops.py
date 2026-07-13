from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from loguru import logger
from patos import FrozenModel
from sqlalchemy import func, inspect, select
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
from .store import TableBase, as_system, verify_rls
from .store.ddl import CreateExtension, Grant, GrantTarget

# Owner-side health counts for the store's principal tables
_MAIN_TABLES = (
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

_SERVING_ENDPOINTS = (
    ("embed", settings.embed_url),
    ("llm", settings.llm_url),
)

_PROBE_TIMEOUT = 2.0


class SetupReport(FrozenModel):
    """What `setup` found already current versus what it applied, the idempotent bootstrap
    read."""

    migrated_from: str | None
    migrated_to: str
    queue_installed: bool


class SchemaHealth(FrozenModel):
    """Alembic migration state, the schema half of a health read."""

    current: str | None
    head: str
    up_to_date: bool


class EndpointHealth(FrozenModel):
    """Reachability of one OpenAI-compatible serving endpoint, the model half of a health
    read."""

    name: str
    url: str
    reachable: bool


class HealthReport(FrozenModel):
    """The engine's operational snapshot, schema, row security, row counts, queue, and
    endpoints."""

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


def run_alembic[**P, T](fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run one blocking alembic `command` call on a dedicated worker thread, returning its
    result."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args, **kwargs).result()


def alembic_head(config: Config) -> str:
    """The revision the installed package's own migration scripts consider current."""
    return ScriptDirectory.from_config(config).get_current_head() or "head"


async def alembic_current() -> str | None:
    """The revision the live database is stamped at, null on a fresh unmigrated database."""
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
    app = create_async_engine(settings.database_url)
    try:
        async with app.connect() as connection:
            return await connection.run_sync(lambda sync: inspect(sync).has_table("pgqueuer"))
    finally:
        await app.dispose()


async def scoped_rls_violations() -> list[str]:
    """Reasons the live schema fails the no-leak contract for any registered scoped table."""
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.connect() as connection:
            return await connection.run_sync(verify_rls)
    finally:
        await admin.dispose()


async def row_counts() -> dict[str, int]:
    """Read every principal table count in one owner-side SQLAlchemy statement."""
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.connect() as connection:
            counts = (
                await connection.execute(
                    select(
                        *(
                            select(func.count())
                            .select_from(TableBase.metadata.tables[table])
                            .scalar_subquery()
                            .label(table)
                            for table in _MAIN_TABLES
                        )
                    )
                )
            ).one()
            return dict(zip(_MAIN_TABLES, counts, strict=True))
    finally:
        await admin.dispose()


async def probe_endpoint(name: str, url: str) -> EndpointHealth:
    """Probe one OpenAI-compatible endpoint's models path with a short timeout."""
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            response = await client.get(f"{url}/models")
        reachable = response.status_code < 500
    except httpx.HTTPError:
        reachable = False
    return EndpointHealth(name=name, url=url, reachable=reachable)


async def grant_app_role_privileges() -> None:
    """Grant the app role CRUD on the public schema, mirroring `initdb/roles.sh` on any
    database."""
    role = settings.app_role
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.begin() as connection:
            for statement in (
                Grant(GrantTarget.schema, "public", role, ("USAGE",)),
                Grant(
                    GrantTarget.all_tables,
                    "public",
                    role,
                    ("SELECT", "INSERT", "UPDATE", "DELETE"),
                ),
                Grant(
                    GrantTarget.all_sequences,
                    "public",
                    role,
                    ("USAGE", "SELECT"),
                ),
                Grant(
                    GrantTarget.default_tables,
                    "public",
                    role,
                    ("SELECT", "INSERT", "UPDATE", "DELETE"),
                ),
                Grant(
                    GrantTarget.default_sequences,
                    "public",
                    role,
                    ("USAGE", "SELECT"),
                ),
            ):
                await connection.execute(statement)
    finally:
        await admin.dispose()


async def enable_query_stats() -> None:
    """Create pg_stat_statements, tolerating a Postgres not yet restarted with the library
    loaded."""
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.begin() as connection:
            await connection.execute(CreateExtension("pg_stat_statements"))
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
    """Bring the database to a ready state, migrate to head, install the queue schema, grant
    CRUD."""
    before = await alembic_current()
    already_queued = await queue_schema_present()
    config = alembic_config()
    run_alembic(command.upgrade, config, "head")
    await install_queue_schema()
    await grant_app_role_privileges()
    await enable_query_stats()
    async with as_system() as session:
        await ontology.refresh(session)
    return SetupReport(
        migrated_from=before, migrated_to=alembic_head(config), queue_installed=not already_queued
    )


async def health() -> HealthReport:
    """Read the engine's schema, row security, row-count, queue, and serving-endpoint state."""
    current = await alembic_current()
    head = alembic_head(alembic_config())
    endpoints = [await probe_endpoint(name, url) for name, url in _SERVING_ENDPOINTS]
    return HealthReport(
        migration=SchemaHealth(current=current, head=head, up_to_date=current == head),
        rls_violations=await scoped_rls_violations(),
        row_counts=await row_counts(),
        queue=await tasks_overview(),
        endpoints=endpoints,
    )
