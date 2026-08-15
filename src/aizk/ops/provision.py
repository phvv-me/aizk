import abc
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from loguru import logger
from patos import FrozenModel
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
from ..store.backend import DatabaseRole, database_adapter
from ..store.ddl import CreateExtension, Grant, GrantTarget
from ..store.identity import User
from ..store.verification import row_security_violations
from .reports import ResetReport, SetupReport


class _DatabaseProvisioning(FrozenModel, abc.ABC):
    """Own the schema lifecycle choices that differ between database backends."""

    queue_table: str
    maintenance_database: str
    migration_versions: Path | None = None

    @abc.abstractmethod
    def drop_database(self, identifier: str) -> str:
        """Build this backend's safe database drop statement."""

    @abc.abstractmethod
    async def finish_schema(self) -> None:
        """Install backend services needed after application grants."""

    @abc.abstractmethod
    async def prepare_schema(self) -> None:
        """Install backend services needed before the application queue."""


class _PostgreSQLProvisioning(_DatabaseProvisioning):
    """Provision PostgreSQL extensions and its PgQueuer runtime."""

    queue_table: str = "pgqueuer"
    maintenance_database: str = "postgres"

    def drop_database(self, identifier: str) -> str:
        return f"DROP DATABASE IF EXISTS {identifier} WITH (FORCE)"

    async def finish_schema(self) -> None:
        await enable_query_stats()

    async def prepare_schema(self) -> None:
        await ensure_bm25_tokenizer()


class _CockroachDBProvisioning(_DatabaseProvisioning):
    """Provision the migration-owned CockroachDB runtime."""

    queue_table: str = "queue_task"
    maintenance_database: str = "defaultdb"
    migration_versions: Path | None = (
        Path(__file__).parent.parent / "store" / "migrations" / "cockroachdb" / "versions"
    )

    def drop_database(self, identifier: str) -> str:
        return f"DROP DATABASE IF EXISTS {identifier} CASCADE"

    async def finish_schema(self) -> None:
        return None

    async def prepare_schema(self) -> None:
        return None


def _database_provisioning() -> _DatabaseProvisioning:
    """Build the configured schema lifecycle strategy."""
    match settings.database_backend:
        case DatabaseBackend.postgresql:
            return _PostgreSQLProvisioning()
        case DatabaseBackend.cockroachdb:
            return _CockroachDBProvisioning()
        case _:
            raise ValueError(f"unsupported database backend {settings.database_backend}")


def alembic_config() -> Config:
    """Build the alembic Config pointed at the migration scripts shipped inside the package."""
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parent.parent / "store" / "migrations")
    )
    # ConfigParser treats percent escapes in encoded passwords and TLS paths as
    # interpolation. Doubling preserves the URL when Alembic reads it back.
    config.set_main_option("sqlalchemy.url", settings.admin_database_url.replace("%", "%%"))
    migration_versions = _database_provisioning().migration_versions
    if migration_versions is not None:
        config.set_main_option(
            "version_locations",
            str(migration_versions),
        )
    return config


def run_alembic[**P, T](fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run one blocking Alembic command in a dedicated worker thread.

    The call returns the command result after the worker finishes.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args, **kwargs).result()


def alembic_head(config: Config) -> str:
    """The revision the installed package's own migration scripts consider current."""
    return ScriptDirectory.from_config(config).get_current_head() or "head"


async def alembic_current() -> str | None:
    """The revision the live database is stamped at, null on a fresh unmigrated database."""
    admin = database_adapter().engine(settings.admin_database_url, DatabaseRole.owner)
    try:
        async with admin.connect() as connection:
            return await connection.run_sync(
                lambda sync: MigrationContext.configure(sync).get_current_revision()
            )
    finally:
        await admin.dispose()


async def has_queue_schema() -> bool:
    """Whether the configured queue backend's durable tables already exist."""
    table = _database_provisioning().queue_table
    app = database_adapter().engine(settings.database_url, DatabaseRole.app)
    try:
        async with app.connect() as connection:
            return await connection.run_sync(lambda sync: inspect(sync).has_table(table))
    finally:
        await app.dispose()


async def grant_app_role_privileges() -> None:
    """Grant application CRUD privileges on the public schema.

    This mirrors `initdb/roles.sh` on either database backend.
    """
    role = settings.app_role
    admin = database_adapter().engine(settings.admin_database_url, DatabaseRole.owner)
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
    """Create `pg_stat_statements` when PostgreSQL is ready for it.

    A server that has not loaded the library yet produces a warning instead.
    """
    admin = database_adapter().engine(settings.admin_database_url, DatabaseRole.owner)
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
    """Bring the configured database to a ready state.

    This migrates to head, prepares backend services, installs the queue, and grants CRUD.
    """
    before = await alembic_current()
    provisioning = _database_provisioning()
    already_queued = await has_queue_schema()
    config = alembic_config()
    run_alembic(command.upgrade, config, "head")
    violations = await row_security_violations()
    if violations:
        raise RuntimeError("row security drift: " + "; ".join(violations))
    await provisioning.prepare_schema()
    await install_queue_schema()
    await grant_app_role_privileges()
    await provisioning.finish_schema()
    async with User.system() as session:
        await Ontology.refresh(session)
    return SetupReport(
        migrated_from=before, migrated_to=alembic_head(config), queue_installed=not already_queued
    )


def _reset_target() -> str:
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
    name = _reset_target()
    identifier = '"' + name.replace('"', '""') + '"'
    provisioning = _database_provisioning()
    maintenance = make_url(settings.admin_database_url).set(
        database=provisioning.maintenance_database
    )
    admin = (
        database_adapter()
        .engine(maintenance, DatabaseRole.owner)
        .execution_options(isolation_level="AUTOCOMMIT")
    )
    try:
        async with admin.connect() as connection:
            await connection.execute(text(provisioning.drop_database(identifier)))
            await connection.execute(text(f"CREATE DATABASE {identifier}"))
    finally:
        await admin.dispose()
    report = await setup()
    return ResetReport(database=name, migrated_to=report.migrated_to)
