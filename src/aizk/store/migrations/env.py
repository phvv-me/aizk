import asyncio

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Importing the store maps models and attaches their RLS declarations.
from aizk.store import TableBase
from alembic import context

config = context.config
target_metadata = TableBase.metadata

_pg_depend = sa.table(
    "pg_depend",
    sa.column("objid", sa.BigInteger()),
    sa.column("refobjid", sa.BigInteger()),
    sa.column("deptype", sa.Text()),
    schema="pg_catalog",
)
_pg_extension = sa.table(
    "pg_extension",
    sa.column("oid", sa.BigInteger()),
    schema="pg_catalog",
)
_pg_class = sa.table(
    "pg_class",
    sa.column("oid", sa.BigInteger()),
    sa.column("relname", sa.Text()),
    schema="pg_catalog",
)


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on an already-open synchronous connection."""

    views = target_metadata.info.get("views", set())
    extension_owned = set(
        connection.execute(
            sa.select(_pg_class.c.relname)
            .join(_pg_depend, _pg_depend.c.objid == _pg_class.c.oid)
            .join(_pg_extension, _pg_depend.c.refobjid == _pg_extension.c.oid)
            .where(_pg_depend.c.deptype == "e")
        ).scalars()
    )

    def include_name(name, type_, parent_names) -> bool:
        """Skip queue-owned tables and mapped views before Alembic reflects their children."""
        if type_ != "table":
            return True
        return not (name.startswith("pgqueuer") or name in views)

    def include_object(object, name, type_, reflected, compare_to) -> bool:
        """Skip reflected objects that live outside the ORM metadata by deliberate design."""
        if type_ == "table" and object.info.get("is_view"):
            return False
        if type_ == "column" and reflected and compare_to is None:
            return not (name == "bm25" and object.table.name == "chunk")
        if type_ == "index" and reflected and compare_to is None:
            return name != "ix_chunk_bm25" and object.table.name not in views
        if type_ == "table" and reflected and compare_to is None and name in views:
            return False
        if type_ != "table" or not reflected or compare_to is not None:
            return True
        return name not in extension_owned

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_name=include_name,
        include_object=include_object,
        autogenerate_plugins=["alembic.autogenerate.*", "rls"],
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run and commit migrations through an async connection."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy."
    )
    async with connectable.begin() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    """Render migrations as PostgreSQL SQL without opening a connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
