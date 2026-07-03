# Adapted from DelfinaCare/rls (https://github.com/DelfinaCare/rls), MIT License,
# Copyright (c) 2024 Delfina Care Inc.
#
# This package ports that project's declarative shape, a model states its own row level security
# policies as `__rls_policies__` on the class, a mapper-construction hook reads them into the
# shared metadata, and alembic ops apply and diff them, rather than depending on the PyPI package
# directly. The port exists because the upstream library never emits FORCE ROW LEVEL SECURITY on
# its alembic path (only its separate `create_policies()`/`create_all()` path does), hardcodes its
# GUC prefix to `rls.` with no override, ships a default per-policy `bypass_rls` escape hatch this
# schema's FORCE-everywhere moat has no use for, and pulls in a hard `starlette` dependency for a
# FastAPI session integration this codebase does not use. `policy.normalize_expression` keeps one
# piece of the upstream algorithm, adapted from `rls._sql_gen.normalize_sql_policy_expression`.
#
# importing ops registers the apply_scoped_rls/drop_scoped_rls/create_scope_policy/
# drop_scope_policy alembic ops, the autogenerate comparator, and the renderers, and importing
# register wires the mapper-construction hook that populates
# `TableBase.metadata.info["rls_policies"]`; both run as a side effect the moment this package is
# imported, the contract `store/migrations/env.py` and every migration rely on by importing
# `aizk.store.rls` before any migration or autogenerate pass runs.
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
from .policy import (
    Command,
    CompiledPolicy,
    Policy,
    compile_expression,
    compile_policy,
    create_statement,
    drop_statement,
)
from .predicates import content_policies, curation_admin_policies, default_scope_policies
from .verify import (
    drifted_policies,
    live_policies,
    live_security,
    policy_matches,
    unprotected_scoped_tables,
    verify_scoped_rls,
)

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
    "content_policies",
    "create_statement",
    "curation_admin_policies",
    "default_scope_policies",
    "drifted_policies",
    "drop_statement",
    "drop_statements",
    "live_policies",
    "live_security",
    "policy_matches",
    "render_apply_scoped_rls",
    "render_create_policy",
    "render_drop_policy",
    "render_drop_scoped_rls",
    "run_apply_scoped_rls",
    "run_create_policy",
    "run_drop_policy",
    "run_drop_scoped_rls",
    "unprotected_scoped_tables",
    "verify_scoped_rls",
]
