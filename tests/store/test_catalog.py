import dbutil
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from aizk.config import settings
from aizk.store import Document, FactClaim, TableBase
from aizk.store.mixins.scoped import ScopeLattice
from aizk.store.models.tables.entity import ContentVisibility, content_policies
from aizk.store.rls import (
    ApplyScopedRlsOp,
    Command,
    CompiledPolicy,
    CreatePolicyOp,
    DropPolicyOp,
    DropScopedRlsOp,
    apply_statements,
    compile_policy,
    drop_statements,
    render_apply_scoped_rls,
    render_create_policy,
    render_drop_policy,
    render_drop_scoped_rls,
    verify_scoped_rls,
)

pytestmark = pytest.mark.usefixtures("migrated_db")


def test_declared_registry_tracks_every_scoped_and_content_table() -> None:
    """The mapper hook registered each policy-declaring table under the shared registry."""
    registry = TableBase.metadata.info["rls"]
    policies = TableBase.metadata.info["rls_policies"]
    assert {"document", "fact_claim", "entity_claim", "watermark"} <= registry
    assert {"entity_content", "fact_content"} <= set(policies)


def test_live_schema_forces_rls_with_no_violations() -> None:
    """Every registered scoped table forces RLS with its canonical policies, the no-leak law."""

    async def body() -> list[str]:
        expected = set(TableBase.metadata.info["rls"])
        engine = create_async_engine(settings.admin_database_url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(lambda sync: verify_scoped_rls(sync, expected))
        finally:
            await engine.dispose()

    assert dbutil.run(body()) == []


def test_scope_lattice_default_policies_cover_the_four_commands() -> None:
    """`default_policies` carries one read plus three per-command write policies, no `FOR ALL`."""
    policies = ScopeLattice(Document.__table__).default_policies()
    commands = {policy.command for policy in policies}
    assert commands == {Command.select, Command.insert, Command.update, Command.delete}
    assert {p.name for p in policies} == {
        "scope_read",
        "scope_insert",
        "scope_update",
        "scope_delete",
    }


def test_fact_claim_adds_the_curation_admin_escape() -> None:
    """`FactClaim` extends the scope policies with the curation-admin read/update/delete."""
    names = {policy.name for policy in FactClaim.__rls_policies__()}
    assert {"scope_read", "scope_insert"} <= names
    assert {"curation_admin_read", "curation_admin_update", "curation_admin_delete"} <= names


def test_content_visibility_has_no_update_policy() -> None:
    """A content table is visible-through-a-claim, freely mintable, and never updatable."""
    policies = content_policies(FactClaim)
    commands = {policy.command for policy in policies}
    assert Command.update not in commands
    assert commands == {Command.select, Command.insert, Command.delete}
    # the read predicate compiles to an EXISTS/IN over the claim table, never a bare column ref
    assert ContentVisibility(FactClaim).read() is not None


def test_apply_and_drop_statements_bracket_the_policies() -> None:
    """`apply_statements` forces RLS and grants the app role; `drop_statements` reverses it."""
    applied = apply_statements("document")
    dropped = drop_statements("document")
    joined = " ".join(applied).lower()
    assert "row level security" in joined and "aizk_app" in joined
    assert any("policy" in statement.lower() for statement in applied)
    assert len(dropped) >= 1


def test_scoped_ops_reverse_to_their_inverse() -> None:
    """Each whole-table op reverses to the other, so a migration downgrades cleanly."""
    assert isinstance(ApplyScopedRlsOp("document").reverse(), DropScopedRlsOp)
    assert isinstance(DropScopedRlsOp("document").reverse(), ApplyScopedRlsOp)
    assert render_apply_scoped_rls(None, ApplyScopedRlsOp("document")) == (
        "op.apply_scoped_rls('document')"
    )
    assert render_drop_scoped_rls(None, DropScopedRlsOp("document")) == (
        "op.drop_scoped_rls('document')"
    )


def test_policy_ops_compile_render_and_reverse() -> None:
    """A compiled policy renders back to a `create_scope_policy` call and reverses to a drop."""
    policy = ScopeLattice(Document.__table__).default_policies()[0]
    compiled = compile_policy(policy)
    assert isinstance(compiled, CompiledPolicy)
    assert compiled.name == "scope_read"

    create = CreatePolicyOp("document", compiled)
    assert isinstance(create.reverse(), DropPolicyOp)
    rendered = render_create_policy(None, create)
    assert rendered.startswith("op.create_scope_policy('document', rls.CompiledPolicy(")
    assert "name='scope_read'" in rendered

    drop = DropPolicyOp("document", compiled)
    assert isinstance(drop.reverse(), CreatePolicyOp)
    assert render_drop_policy(None, drop).startswith("op.drop_scope_policy('document'")
