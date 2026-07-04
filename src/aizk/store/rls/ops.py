import rls
from sqlalchemy import MetaData

from alembic.autogenerate import comparators, renderers
from alembic.autogenerate.api import AutogenContext
from alembic.operations import MigrateOperation, Operations
from alembic.operations.ops import UpgradeOps

from ..mixins.base import TableBase

# aizk's own thin Alembic operation layer: table-name-only ops backed by the standalone `rls`
# library (https://github.com/phvv-me/rls, itself forked from DelfinaCare/rls).
#
# `rls`'s own `ApplyRlsOp`/`DropRlsOp` carry their compiled policies inline so a migration is
# self-contained without a metadata lookup at run time (see `rls.ops.operations`), a design this
# schema's own historical migration (`0001_init.py`) predates: it calls
# `op.apply_scoped_rls(table)`/`op.drop_scoped_rls(table)` with only a table name, expecting the
# policies read fresh from `TableBase.metadata.info["rls_policies"][table]` at invoke time. That
# call shape is a permanent part of a committed migration file, so this module keeps the
# table-name-only ops alive as aizk's own, assembling their DDL from `rls.enable_statements`/
# `rls.disable_statements`/`rls.compile_policy` rather than reimplementing DDL assembly, and
# reusing `rls.unprotected_tables`/`rls.drifted_policies` for the autogenerate comparator's diff.
#
# importing this module registers apply_scoped_rls/drop_scoped_rls/create_scope_policy/
# drop_scope_policy on Alembic's `op` proxy, plus the compare_scoped_rls autogenerate comparator
# and its renderers, the contract `store/migrations/env.py` and `0001_init.py` rely on by
# importing `aizk.store.rls` before any migration or autogenerate pass runs.

# the non-superuser, non-bypassrls login role the application connects as, so row level security is
# enforced on every read and write rather than silently bypassed by the table owner.
APP_ROLE = "aizk_app"


def _declared(table: str) -> list[rls.Policy]:
    """The policies currently declared for `table`, read fresh from the shared registry."""
    return TableBase.metadata.info["rls_policies"][table]


def apply_statements(table: str, grant: bool = True) -> list[str]:
    """Force row level security on `table` with its declared policies, in declaration order.

    table: table to protect, already registered by one of its columns' owning model.
    grant: also grant the app role CRUD, skipped when the app role does not exist yet.
    """
    compiled = [rls.compile_policy(policy) for policy in _declared(table)]
    return rls.enable_statements(table, compiled, grant_role=APP_ROLE if grant else None)


def drop_statements(table: str, grant: bool = True) -> list[str]:
    """Reverse `apply_statements` for `table`, dropping every declared policy in reverse order.

    table: table to unprotect.
    grant: also revoke the app role CRUD, matching how `apply_statements` granted it.
    """
    compiled = [rls.compile_policy(policy) for policy in _declared(table)]
    return rls.disable_statements(table, compiled, grant_role=APP_ROLE if grant else None)


@Operations.register_operation("apply_scoped_rls")
class ApplyScopedRlsOp(MigrateOperation):
    """Force every declared policy on one Scoped table, the whole-table bootstrap op."""

    def __init__(self, table: str, grant: bool = True) -> None:
        self.table = table
        self.grant = grant

    @classmethod
    def apply_scoped_rls(cls, operations: Operations, table: str, grant: bool = True) -> None:
        """Invoke from a migration as `op.apply_scoped_rls(table)`."""
        operations.invoke(cls(table, grant))

    def reverse(self) -> DropScopedRlsOp:
        return DropScopedRlsOp(self.table, self.grant)


@Operations.register_operation("drop_scoped_rls")
class DropScopedRlsOp(MigrateOperation):
    """Reverse `apply_scoped_rls`, dropping the policies and disabling row level security."""

    def __init__(self, table: str, grant: bool = True) -> None:
        self.table = table
        self.grant = grant

    @classmethod
    def drop_scoped_rls(cls, operations: Operations, table: str, grant: bool = True) -> None:
        """Invoke from a migration as `op.drop_scoped_rls(table)`."""
        operations.invoke(cls(table, grant))

    def reverse(self) -> ApplyScopedRlsOp:
        return ApplyScopedRlsOp(self.table, self.grant)


@Operations.implementation_for(ApplyScopedRlsOp)
def run_apply_scoped_rls(operations: Operations, operation: ApplyScopedRlsOp) -> None:
    """Emit the apply DDL when a migration invokes `apply_scoped_rls`."""
    for statement in apply_statements(operation.table, operation.grant):
        operations.execute(statement)


@Operations.implementation_for(DropScopedRlsOp)
def run_drop_scoped_rls(operations: Operations, operation: DropScopedRlsOp) -> None:
    """Emit the drop DDL when a migration invokes `drop_scoped_rls`."""
    for statement in drop_statements(operation.table, operation.grant):
        operations.execute(statement)


@Operations.register_operation("create_scope_policy")
class CreatePolicyOp(MigrateOperation):
    """Create or replace one compiled policy on a table, the autogenerate differ's fine-grained op.

    Idempotent: implemented as a drop-if-exists followed by the create, so it covers both a
    genuinely missing policy and one whose clause drifted from its declaration under the same op.
    """

    def __init__(self, table: str, policy: rls.CompiledPolicy) -> None:
        self.table = table
        self.policy = policy

    @classmethod
    def create_scope_policy(
        cls, operations: Operations, table: str, policy: rls.CompiledPolicy
    ) -> None:
        """Invoke from a migration as `op.create_scope_policy(table, policy)`."""
        operations.invoke(cls(table, policy))

    def reverse(self) -> DropPolicyOp:
        return DropPolicyOp(self.table, self.policy)


@Operations.register_operation("drop_scope_policy")
class DropPolicyOp(MigrateOperation):
    """Drop one named policy, carrying its compiled definition so the op reverses cleanly."""

    def __init__(self, table: str, policy: rls.CompiledPolicy) -> None:
        self.table = table
        self.policy = policy

    @classmethod
    def drop_scope_policy(
        cls, operations: Operations, table: str, policy: rls.CompiledPolicy
    ) -> None:
        """Invoke from a migration as `op.drop_scope_policy(table, policy)`."""
        operations.invoke(cls(table, policy))

    def reverse(self) -> CreatePolicyOp:
        return CreatePolicyOp(self.table, self.policy)


@Operations.implementation_for(CreatePolicyOp)
def run_create_policy(operations: Operations, operation: CreatePolicyOp) -> None:
    """Drop any same-named policy then create the compiled definition, in one statement pair."""
    operations.execute(rls.drop_statement(operation.table, operation.policy.name))
    operations.execute(rls.create_statement(operation.table, operation.policy))


@Operations.implementation_for(DropPolicyOp)
def run_drop_policy(operations: Operations, operation: DropPolicyOp) -> None:
    """Drop the named policy."""
    operations.execute(rls.drop_statement(operation.table, operation.policy.name))


def _import_rls(autogen_context: AutogenContext | None) -> None:
    """Add the `rls` import an autogenerated migration needs to reference `alembic_ops.RLSOp`."""
    if autogen_context is not None:
        autogen_context.imports.add("from aizk.store import rls")


@renderers.dispatch_for(ApplyScopedRlsOp)
def render_apply_scoped_rls(autogen_context: AutogenContext | None, op: ApplyScopedRlsOp) -> str:
    """Render an emitted apply op back into migration source."""
    return f"op.apply_scoped_rls({op.table!r})"


@renderers.dispatch_for(DropScopedRlsOp)
def render_drop_scoped_rls(autogen_context: AutogenContext | None, op: DropScopedRlsOp) -> str:
    """Render an emitted drop op back into migration source."""
    return f"op.drop_scoped_rls({op.table!r})"


def _render_compiled_policy(policy: rls.CompiledPolicy) -> str:
    """The `rls.CompiledPolicy(...)` constructor call one op's rendering embeds."""
    return (
        f"rls.CompiledPolicy(name={policy.name!r}, command=rls.Command.{policy.command.name}, "
        f"using={policy.using!r}, check={policy.check!r})"
    )


@renderers.dispatch_for(CreatePolicyOp)
def render_create_policy(autogen_context: AutogenContext | None, op: CreatePolicyOp) -> str:
    """Render an emitted create-policy op back into migration source."""
    _import_rls(autogen_context)
    return f"op.create_scope_policy({op.table!r}, {_render_compiled_policy(op.policy)})"


@renderers.dispatch_for(DropPolicyOp)
def render_drop_policy(autogen_context: AutogenContext | None, op: DropPolicyOp) -> str:
    """Render an emitted drop-policy op back into migration source."""
    _import_rls(autogen_context)
    return f"op.drop_scope_policy({op.table!r}, {_render_compiled_policy(op.policy)})"


@comparators.dispatch_for("schema")
def compare_scoped_rls(
    autogen_context: AutogenContext, upgrade_ops: UpgradeOps, schemas: set[str | None]
) -> None:
    """Make autogenerate close any gap between the declared policies and the live catalog.

    A table with no FORCE or no row security at all gets the whole-table `ApplyScopedRlsOp`
    bootstrap, the shape a brand-new Scoped model or a force-stripped table both need. A table
    already protected gets the fine-grained differ instead. `rls.drifted_policies` compares each
    declared policy's compiled, normalized clause against the live catalog's, so only the policies
    that actually changed are dropped and recreated, and any live policy no longer declared is
    dropped on its own.

    autogen_context: alembic's autogenerate context, carrying the connection and target metadata.
    upgrade_ops: the operation list this pass appends to.
    schemas: unused, part of the comparator hook's fixed signature.
    """
    connection = autogen_context.connection
    metadata = autogen_context.metadata
    if connection is None or metadata is None:
        return
    catalogs = [metadata] if isinstance(metadata, MetaData) else metadata
    declared = {
        table: policies
        for catalog in catalogs
        for table, policies in catalog.info.get("rls_policies", {}).items()
    }
    if not declared:
        return
    bootstrap = set(rls.unprotected_tables(connection, set(declared)))
    for table in sorted(bootstrap):
        upgrade_ops.ops.append(ApplyScopedRlsOp(table))
    for table in sorted(declared.keys() - bootstrap):
        changed, stale = rls.drifted_policies(connection, table, declared[table])
        for compiled in stale:
            upgrade_ops.ops.append(DropPolicyOp(table, compiled))
        for policy in changed:
            upgrade_ops.ops.append(CreatePolicyOp(table, rls.compile_policy(policy)))
