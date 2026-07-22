from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from loguru import logger
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from ..background.queue import install_queue_schema
from ..backup import ensure_bm25_tokenizer
from ..config import DatabaseBackend, settings
from ..ontology import Ontology
from ..store.backend import database_adapter
from ..store.ddl import CreateExtension, Grant, GrantTarget
from ..store.identity import User
from .reports import ResetReport, SetupReport


def alembic_config() -> Config:
    """Build the alembic Config pointed at the migration scripts shipped inside the package."""
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parent.parent / "store" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", settings.admin_database_url)
    if settings.database_backend is DatabaseBackend.cockroachdb:
        config.set_main_option(
            "version_locations",
            str(
                Path(__file__).parent.parent / "store" / "migrations" / "cockroachdb" / "versions"
            ),
        )
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
    admin = database_adapter().engine(settings.admin_database_url, False)
    try:
        async with admin.connect() as connection:
            return await connection.run_sync(
                lambda sync: MigrationContext.configure(sync).get_current_revision()
            )
    finally:
        await admin.dispose()


async def queue_schema_present() -> bool:
    """Whether the configured queue backend's durable tables already exist."""
    table = (
        "queue_task" if settings.database_backend is DatabaseBackend.cockroachdb else "pgqueuer"
    )
    app = database_adapter().engine(settings.database_url, True)
    try:
        async with app.connect() as connection:
            return await connection.run_sync(lambda sync: inspect(sync).has_table(table))
    finally:
        await app.dispose()


async def grant_app_role_privileges() -> None:
    """Grant the app role CRUD on the public schema, mirroring `initdb/roles.sh` on any
    database."""
    role = settings.app_role
    admin = database_adapter().engine(settings.admin_database_url, False)
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
    admin = database_adapter().engine(settings.admin_database_url, False)
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
    if settings.database_backend is DatabaseBackend.postgresql:
        await ensure_bm25_tokenizer()
    await install_queue_schema()
    await grant_app_role_privileges()
    if settings.database_backend is DatabaseBackend.postgresql:
        await enable_query_stats()
    async with User.system() as session:
        await Ontology.refresh(session)
    return SetupReport(
        migrated_from=before, migrated_to=alembic_head(config), queue_installed=not already_queued
    )


def reset_target() -> str:
    """The one database both configured DSNs name, the only database reset may drop.

    Setup migrates whatever `admin_database_url` names, so the drop must target that same
    database, never a maintenance database and never one the application DSN disagrees on.
    """
    name = make_url(settings.admin_database_url).database
    if name is None or name in {"defaultdb", "postgres"} or name.startswith("template"):
        raise ValueError(f"refusing to reset maintenance database {name!r}")
    if make_url(settings.database_url).database != name:
        raise ValueError("database_url and admin_database_url must name the same database")
    return name


async def reset() -> ResetReport:
    """Recreate only the configured Aizk database, then install its complete schema."""
    name = reset_target()
    identifier = '"' + name.replace('"', '""') + '"'
    cockroach = settings.database_backend is DatabaseBackend.cockroachdb
    maintenance = make_url(settings.admin_database_url).set(
        database="defaultdb" if cockroach else "postgres"
    )
    admin = (
        database_adapter()
        .engine(maintenance, False)
        .execution_options(isolation_level="AUTOCOMMIT")
    )
    try:
        async with admin.connect() as connection:
            drop = (
                f"DROP DATABASE IF EXISTS {identifier} CASCADE"
                if cockroach
                else f"DROP DATABASE IF EXISTS {identifier} WITH (FORCE)"
            )
            await connection.execute(text(drop))
            await connection.execute(text(f"CREATE DATABASE {identifier}"))
    finally:
        await admin.dispose()
    report = await setup()
    return ResetReport(database=name, migrated_to=report.migrated_to)
