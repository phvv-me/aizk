from dataclasses import dataclass, field
from types import SimpleNamespace

import dbutil
import pytest
from rls import (
    ApplyScopedRlsOp,
    CreatePolicyOp,
    DropPolicyOp,
    DropScopedRlsOp,
    compare_scoped_rls,
    compile_policy,
    run_apply_scoped_rls,
    run_create_policy,
    run_drop_policy,
    run_drop_scoped_rls,
    scoped_apply_statements,
    scoped_drop_statements,
)
from sqlalchemy.ext.asyncio import create_async_engine

from aizk.config import settings
from aizk.store import Document, TableBase
from aizk.store.mixins.scoped import ScopeLattice
from alembic.operations.ops import UpgradeOps

pytestmark = pytest.mark.usefixtures("migrated_db")


@dataclass
class RecordingOps:
    """A recording stand-in for alembic's `Operations`, capturing executed DDL and invoked ops."""

    executed: list[str] = field(default_factory=list)
    invoked: list[object] = field(default_factory=list)

    def execute(self, statement: str) -> None:
        """Record one emitted DDL statement."""
        self.executed.append(statement)

    def invoke(self, operation: object) -> None:
        """Record one invoked migration operation."""
        self.invoked.append(operation)


def compiled_read_policy() -> object:
    """The compiled `scope_read` policy for `document`, the fine-grained ops' payload."""
    return compile_policy(ScopeLattice(Document.__table__).default_policies()[0])


def test_apply_and_drop_statements_honor_the_grant_flag() -> None:
    """The grant flag toggles whether the app-role CRUD grant rides alongside the RLS DDL."""
    with_grant = " ".join(scoped_apply_statements("document", grant=True)).lower()
    without_grant = " ".join(scoped_apply_statements("document", grant=False)).lower()
    assert "aizk_app" in with_grant and "aizk_app" not in without_grant
    assert len(scoped_drop_statements("document", grant=False)) >= 1


def test_run_apply_and_drop_scoped_rls_emit_their_ddl() -> None:
    """The whole-table run implementations emit the compiled force/grant and reverse DDL."""
    ops = RecordingOps()
    run_apply_scoped_rls(ops, ApplyScopedRlsOp("document"))
    assert any("row level security" in statement.lower() for statement in ops.executed)
    ops = RecordingOps()
    run_drop_scoped_rls(ops, DropScopedRlsOp("document"))
    assert ops.executed


def test_run_policy_ops_emit_drop_then_create() -> None:
    """`run_create_policy` drops any same-named policy then creates it; the drop op emits one."""
    ops = RecordingOps()
    run_create_policy(ops, CreatePolicyOp("document", compiled_read_policy()))
    assert len(ops.executed) == 2  # idempotent drop-if-exists then create
    ops = RecordingOps()
    run_drop_policy(ops, DropPolicyOp("document", compiled_read_policy()))
    assert len(ops.executed) == 1


def test_op_classmethods_invoke_their_operation() -> None:
    """The `op.<name>(...)` entrypoints invoke the matching operation onto the operations proxy."""
    ops = RecordingOps()
    ApplyScopedRlsOp.apply_scoped_rls(ops, "document")
    DropScopedRlsOp.drop_scoped_rls(ops, "document")
    CreatePolicyOp.create_rls_policy(ops, "document", compiled_read_policy())
    DropPolicyOp.drop_rls_policy(ops, "document", compiled_read_policy())
    kinds = [type(op) for op in ops.invoked]
    assert kinds == [ApplyScopedRlsOp, DropScopedRlsOp, CreatePolicyOp, DropPolicyOp]


def test_render_adds_the_rls_import_to_a_real_autogen_context() -> None:
    """Rendering a policy op into migration source records the `rls` import the migration needs."""
    from rls import render_create_policy

    context = SimpleNamespace(imports=set())
    render_create_policy(context, CreatePolicyOp("document", compiled_read_policy()))
    assert "import rls" in context.imports


def test_compare_returns_early_when_no_policies_are_declared() -> None:
    """With an empty metadata carrying no declared policies, the comparator emits nothing."""
    from sqlalchemy import MetaData

    upgrade_ops = UpgradeOps(ops=[])
    context = SimpleNamespace(connection=object(), metadata=MetaData())
    compare_scoped_rls(context, upgrade_ops, set())
    assert upgrade_ops.ops == []


def test_compare_returns_early_without_a_connection() -> None:
    """The autogenerate comparator is a no-op when the context carries no live connection."""
    upgrade_ops = UpgradeOps(ops=[])
    context = SimpleNamespace(connection=None, metadata=TableBase.metadata)
    compare_scoped_rls(context, upgrade_ops, set())
    assert upgrade_ops.ops == []


async def compare_under_mutation(mutations: list[str]) -> list[str]:
    """Apply catalog mutations in a rolled-back transaction and return the op class names compare
    emits.

    mutations: raw DDL applied before running the comparator, never committed.
    """
    engine = create_async_engine(settings.admin_database_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()

            def run(sync_connection: object) -> list[str]:
                for statement in mutations:
                    sync_connection.exec_driver_sql(statement)
                upgrade_ops = UpgradeOps(ops=[])
                context = SimpleNamespace(connection=sync_connection, metadata=TableBase.metadata)
                compare_scoped_rls(context, upgrade_ops, set())
                return [type(op).__name__ for op in upgrade_ops.ops]

            names = await connection.run_sync(run)
            await transaction.rollback()
            return names
    finally:
        await engine.dispose()


def test_compare_recreates_a_drifted_policy_and_drops_a_stale_one() -> None:
    """A missing declared policy recreates and an undeclared live policy drops."""
    names = dbutil.run(
        compare_under_mutation(
            [
                "DROP POLICY scope_read ON document",
                "CREATE POLICY extra_undeclared ON document FOR SELECT USING (true)",
            ]
        )
    )
    assert "CreatePolicyOp" in names  # scope_read recreated from its declaration
    assert "DropPolicyOp" in names  # the undeclared live policy dropped


def test_compare_bootstraps_a_table_that_lost_row_security() -> None:
    """A scoped table with row security disabled gets the whole-table apply bootstrap op."""
    names = dbutil.run(compare_under_mutation(["ALTER TABLE document DISABLE ROW LEVEL SECURITY"]))
    assert "ApplyScopedRlsOp" in names


def test_compare_finds_no_drift_against_the_live_catalog() -> None:
    """Against the migrated schema, the comparator emits no ops, the no-drift no-leak guarantee."""

    async def body() -> list[object]:
        engine = create_async_engine(settings.admin_database_url)
        try:
            async with engine.connect() as connection:

                def run(sync_connection: object) -> list[object]:
                    upgrade_ops = UpgradeOps(ops=[])
                    context = SimpleNamespace(
                        connection=sync_connection, metadata=TableBase.metadata
                    )
                    compare_scoped_rls(context, upgrade_ops, set())
                    return upgrade_ops.ops

                return await connection.run_sync(run)
        finally:
            await engine.dispose()

    assert dbutil.run(body()) == []
