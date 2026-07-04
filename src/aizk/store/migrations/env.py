import asyncio

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# importing the rls module registers the apply_scoped_rls/drop_scoped_rls alembic operations the
# migrations call and the autogenerate comparator that guards every Scoped table, so the import has
# to run before any migration or autogenerate pass executes.
from aizk.store import TableBase
from aizk.store import rls as rls
from alembic import context

config = context.config
target_metadata = TableBase.metadata


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on an already-open synchronous connection.

    connection: connection handed over by the async runner.
    """

    def include_object(object, name, type_, reflected, compare_to) -> bool:
        """Skip reflected objects that live outside the ORM metadata by deliberate design.

        Autogenerate reflects every table in the target schema, including ones an extension like
        pg_tokenizer creates for its own bookkeeping (tokenizer, model, synonym, stopwords), so
        without this filter a diff against our ORM metadata misreads them as dropped tables. The
        vchord_bm25 lane's `chunk.bm25` column and its index are the one deliberately-unmapped
        column in the schema, a bm25vector type the ORM has no ann for, kept in sync by a
        migration-owned trigger and read only through a text() statement, so they are excluded the
        same way rather than misread as drift. Every `ViewBase` view (`live_fact` today) is
        hand-written `CREATE VIEW` DDL, reflected as an ordinary table since autogenerate cannot
        tell a view from a table at all; the ORM side is excluded by its own `info={"is_view":
        True}` tag, while the reflected side, a plain `Table` carrying no such info, is excluded
        by the `views` name set `store.mixins.view.register_view` stamps onto the shared metadata,
        so a future view needs no edit here.
        """
        views = target_metadata.info.get("views", set())
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
        owned = connection.execute(
            text(
                "SELECT 1 FROM pg_depend d "
                "JOIN pg_extension e ON d.refobjid = e.oid "
                "JOIN pg_class c ON d.objid = c.oid "
                "WHERE c.relname = :name AND d.deptype = 'e'"
            ),
            {"name": name},
        ).first()
        return owned is None

    context.configure(
        connection=connection, target_metadata=target_metadata, include_object=include_object
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Open an async engine and run the migrations through a sync bridge."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy."
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


asyncio.run(run_migrations_online())
