import dbutil
import pytest
import rls
from rls.alembic import AlterRLSOp
from rls.alembic.autogen import compare_rls
from rls.ddl import RLSAction, RLSStatement
from sqlalchemy import MetaData
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import ExecutableDDLElement

from aizk.config import settings
from aizk.store import Document, TableBase
from alembic.autogenerate.api import AutogenContext
from alembic.migration import MigrationContext
from alembic.operations.ops import UpgradeOps

pytestmark = pytest.mark.usefixtures("migrated_db")


def autogen_context(metadata: MetaData, connection: Connection | None = None) -> AutogenContext:
    migration = (
        MigrationContext.configure(connection=connection)
        if connection is not None
        else MigrationContext.configure(dialect_name="postgresql")
    )
    return AutogenContext(migration, metadata)


async def compare_under_mutation(
    mutations: tuple[ExecutableDDLElement, ...],
) -> list[AlterRLSOp]:
    engine = create_async_engine(settings.admin_database_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()

            def run(sync_connection: Connection) -> list[AlterRLSOp]:
                for statement in mutations:
                    sync_connection.execute(statement)
                operations = UpgradeOps(ops=[])
                compare_rls(autogen_context(TableBase.metadata, sync_connection), operations)
                return [
                    operation for operation in operations.ops if isinstance(operation, AlterRLSOp)
                ]

            operations = await connection.run_sync(run)
            await transaction.rollback()
            return operations
    finally:
        await engine.dispose()


def test_comparator_replaces_drift_as_one_complete_transition() -> None:
    operations = dbutil.run(
        compare_under_mutation(
            (
                RLSStatement(Document.__table__, RLSAction.drop, name="scope_read"),
                RLSStatement(
                    Document.__table__,
                    RLSAction.create,
                    policy=rls.CompiledPolicy(
                        name="extra_undeclared",
                        command=rls.Command.select,
                        using="true",
                    ),
                ),
            )
        )
    )
    document = next(operation for operation in operations if operation.table_name == "document")
    assert document.before is not None
    assert document.after is not None
    assert {policy.name for policy in document.before.policies} == {
        "extra_undeclared",
        "scope_insert",
        "scope_update",
    }
    assert {policy.name for policy in document.after.policies} == {
        "scope_read",
        "scope_insert",
        "scope_update",
    }


def test_comparator_bootstraps_a_table_that_lost_row_security() -> None:
    operations = dbutil.run(
        compare_under_mutation((RLSStatement(Document.__table__, RLSAction.disable),))
    )
    document = next(operation for operation in operations if operation.table_name == "document")
    assert document.after is not None


def test_comparator_finds_no_drift_against_the_live_catalog() -> None:
    assert dbutil.run(compare_under_mutation(())) == []
