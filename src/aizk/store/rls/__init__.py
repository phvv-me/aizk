from rls import (
    Command,
    CompiledPolicy,
    Policy,
    compile_expression,
    compile_policy,
    create_statement,
    drop_statement,
    verify_rls,
)
from sqlalchemy.engine import Connection

from ..mixins.base import TableBase
from . import register as register
from .ops import (
    APP_ROLE,
    ApplyScopedRlsOp,
    CreatePolicyOp,
    DropPolicyOp,
    DropScopedRlsOp,
    apply_statements,
    compare_scoped_rls,
    drop_statements,
    render_apply_scoped_rls,
    render_create_policy,
    render_drop_policy,
    render_drop_scoped_rls,
    run_apply_scoped_rls,
    run_create_policy,
    run_drop_policy,
    run_drop_scoped_rls,
)

# Built on the standalone `rls` library (https://github.com/phvv-me/rls, itself forked from
# DelfinaCare/rls) for policy compilation, DDL assembly, sqlglot-based clause comparison, and
# catalog verification. This package stays generic glue over that library: `register.py`'s
# mapper-construction hook (which tracks every policy-declaring table under
# `metadata.info["rls"]`, the autogenerate guard set `Scoped` and content tables alike rely on)
# and `ops.py`'s table-name-only `apply_scoped_rls`/`drop_scoped_rls` ops, kept exactly as the
# committed `0001_init.py` migration already calls them rather than switching to the library's own
# `apply_rls`/`drop_rls` (which carry their policies inline, a call shape that migration
# predates). The actual visibility lattice, `ScopeLattice`, lives beside `Scoped`, the one mixin
# that builds policies from it (`store.mixins.scoped`), and content-table visibility lives
# beside its two consumers (`store.models.tables.entity`'s `content_policies`). Nothing in this
# package is aizk-domain-specific.
#
# importing ops registers the apply_scoped_rls/drop_scoped_rls/create_scope_policy/
# drop_scope_policy alembic ops, the autogenerate comparator, and the renderers, and importing
# register wires the mapper-construction hook that populates
# `TableBase.metadata.info["rls_policies"]`; both run as a side effect the moment this package is
# imported, the contract `store/migrations/env.py` and every migration rely on by importing
# `aizk.store.rls` before any migration or autogenerate pass runs.


def verify_scoped_rls(
    connection: Connection, expected: set[str], declared: dict[str, list[Policy]] | None = None
) -> list[str]:
    """Reasons the live schema fails the no-leak contract for any expected scoped table.

    connection: synchronous connection used to read the catalog.
    expected: table names every Scoped model registered in `metadata.info['rls']`.
    declared: `table -> policies` to verify against, the live registry's own
        `TableBase.metadata.info["rls_policies"]` by default. A test passes its own mapping to
        verify a synthetic probe table that carries no mapped model of its own.
    """
    policy_registry = declared if declared is not None else TableBase.metadata.info["rls_policies"]
    return verify_rls(connection, expected, declared=policy_registry)


__all__ = [
    "APP_ROLE",
    "ApplyScopedRlsOp",
    "Command",
    "CompiledPolicy",
    "CreatePolicyOp",
    "DropPolicyOp",
    "DropScopedRlsOp",
    "Policy",
    "apply_statements",
    "compare_scoped_rls",
    "compile_expression",
    "compile_policy",
    "create_statement",
    "drop_statement",
    "drop_statements",
    "render_apply_scoped_rls",
    "render_create_policy",
    "render_drop_policy",
    "render_drop_scoped_rls",
    "run_apply_scoped_rls",
    "run_create_policy",
    "run_drop_policy",
    "run_drop_scoped_rls",
    "verify_scoped_rls",
]
