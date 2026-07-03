import asyncio
import io
import uuid
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit

import pytest
from graphdb import drop_principals, purge_owner
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from sqlalchemy import (
    MetaData,
    Uuid,
    and_,
    bindparam,
    column,
    exists,
    func,
    literal,
    or_,
    select,
    text,
)
from sqlalchemy import table as sa_table
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from strategies import scope_principals

from aizk.cli import migrate
from aizk.config import settings
from aizk.extract.models import TimedFact
from aizk.graph.build import GraphWriter
from aizk.graph.ids import entity_id, fact_id
from aizk.store import (
    Chunk,
    Document,
    FactClaim,
    NoTenantContext,
    TableBase,
    acting_as,
    async_session,
    rls,
)
from alembic.autogenerate.api import AutogenContext
from alembic.migration import MigrationContext
from alembic.operations import MigrateOperation, Operations
from alembic.operations.ops import UpgradeOps


class ProbeResult(NamedTuple):
    """Outcome of one adversarial visibility probe across two principal.

    a_sees: document ids principal A could read.
    b_sees: document ids principal B could read.
    anon_count: document visible with no app.uid set, which must be zero.
    a_private: id of the document A owns privately.
    b_private: id of the document B owns privately.
    t_shared: id of the document B owns but shares with group T.
    """

    a_sees: set[uuid.UUID]
    b_sees: set[uuid.UUID]
    anon_count: int
    a_private: uuid.UUID
    b_private: uuid.UUID
    t_shared: uuid.UUID


async def provision(principal_a: uuid.UUID, principal_b: uuid.UUID, group_t: uuid.UUID) -> None:
    """Migrate to head and seed two principals plus a bridging group.

    Both principals join group T, A as a reader and B as a writer, so T is the only bridge between
    them and B is the one allowed to publish into it. The app connects as a non-superuser role, so
    row level security is already in force and no test-only role is needed.

    principal_a: identity that will own a private document and read group T.
    principal_b: identity that will own a private document and write a T-shared one.
    group_t: shared scope bridging A and B.
    """
    migrate()
    async with async_session()() as session, session.begin():
        await session.execute(
            text("INSERT INTO principal (id) VALUES (:a), (:b)"),
            {"a": principal_a, "b": principal_b},
        )
        await session.execute(
            text("INSERT INTO group_ (id, name) VALUES (:t, :name)"),
            {"t": group_t, "name": f"rls test team {group_t}"},
        )
        await session.execute(
            text(
                "INSERT INTO membership (principal_id, group_id, role) "
                "VALUES (:a, :t, 'reader'), (:b, :t, 'writer')"
            ),
            {"a": principal_a, "b": principal_b, "t": group_t},
        )


async def insert_doc(
    principal: uuid.UUID,
    doc_id: uuid.UUID,
    owner: uuid.UUID,
    scope: uuid.UUID | None,
) -> None:
    """Insert one document while acting as a principal, under the write-check policy.

    principal: identity the session acts under.
    doc_id: id of the document to create.
    owner: principal that owns the new row.
    scope: group the row is shared with, or None for private.
    """
    async with acting_as(principal) as session:
        await session.execute(
            text(
                "INSERT INTO document (id, content_hash, owner_id, scope) "
                "VALUES (:id, :hash, :owner, :scope)"
            ),
            {"id": doc_id, "hash": "rls", "owner": owner, "scope": scope},
        )


async def visible_ids(principal: uuid.UUID, candidates: list[uuid.UUID]) -> set[uuid.UUID]:
    """Document ids among candidates that a principal can read under row level security.

    principal: identity the session acts under.
    candidates: document ids to probe for visibility.
    """
    async with acting_as(principal) as session:
        result = await session.execute(
            text("SELECT id FROM document WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": candidates},
        )
        return set(result.scalars())


async def anon_visible_count(candidates: list[uuid.UUID]) -> int:
    """Candidate documents visible to the app role with no app.uid set, which must be zero.

    candidates: document ids to probe for visibility.
    """
    async with async_session()() as session, session.begin():
        result = await session.execute(
            text("SELECT count(*) FROM document WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": candidates},
        )
        return result.scalar_one()


async def cleanup(
    principal_a: uuid.UUID,
    principal_b: uuid.UUID,
    group_t: uuid.UUID,
    a_private: uuid.UUID,
    b_private: uuid.UUID,
    t_shared: uuid.UUID,
) -> None:
    """Remove every created row, deleting documents as their owner so the write policy permits it.

    principal_a: first seeded principal, owner of a_private.
    principal_b: second seeded principal, owner of b_private and t_shared.
    group_t: seeded group whose membership and row to delete.
    a_private: document owned by A to delete.
    b_private: document owned by B to delete.
    t_shared: document owned by B and shared with T to delete.
    """
    async with acting_as(principal_a) as session:
        await session.execute(text("DELETE FROM document WHERE id = :id"), {"id": a_private})
    async with acting_as(principal_b) as session:
        await session.execute(
            text("DELETE FROM document WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": [b_private, t_shared]},
        )
    async with async_session()() as session, session.begin():
        await session.execute(text("DELETE FROM membership WHERE group_id = :t"), {"t": group_t})
        await session.execute(text("DELETE FROM group_ WHERE id = :t"), {"t": group_t})
        await session.execute(
            text("DELETE FROM principal WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": [principal_a, principal_b]},
        )


async def leak_probe(
    principal_a: uuid.UUID, principal_b: uuid.UUID, group_t: uuid.UUID
) -> ProbeResult:
    """Seed the lattice, plant three documents, and read them back as each principal.

    A owns a private document and belongs to T, B owns a private document and one shared with T,
    so a correct lattice lets each principal see only their own row plus the T-shared one. Cleanup
    runs in a finally block so a failed assertion never leaves rows behind.

    principal_a: identity that owns a private document and belongs to group T.
    principal_b: identity that owns a private document and one shared with T.
    group_t: the only group bridging A and B.
    """
    a_private, b_private, t_shared = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    candidates = [a_private, b_private, t_shared]
    await provision(principal_a, principal_b, group_t)
    try:
        await insert_doc(principal_a, a_private, principal_a, None)
        await insert_doc(principal_b, b_private, principal_b, None)
        await insert_doc(principal_b, t_shared, principal_b, group_t)
        return ProbeResult(
            a_sees=await visible_ids(principal_a, candidates),
            b_sees=await visible_ids(principal_b, candidates),
            anon_count=await anon_visible_count(candidates),
            a_private=a_private,
            b_private=b_private,
            t_shared=t_shared,
        )
    finally:
        await cleanup(principal_a, principal_b, group_t, a_private, b_private, t_shared)


@hypothesis_settings(
    max_examples=8, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(principals=scope_principals())
def test_rls_blocks_cross_principal_reads(
    requires_db: None, principals: tuple[uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    """For any two principals bridged by one group, neither reads the other's private row.

    A reads only its own row plus the T-shared one, B reads only its own plus the shared one, and
    an anonymous session with no app.uid set reads nothing. The property quantifies the no-leak
    contract over arbitrary identities rather than asserting it for one hand-fixed triple.
    """
    principal_a, principal_b, group_t = principals
    result = asyncio.run(leak_probe(principal_a, principal_b, group_t))

    assert result.a_sees == {result.a_private, result.t_shared}
    assert result.b_private not in result.a_sees

    assert result.b_sees == {result.b_private, result.t_shared}
    assert result.a_private not in result.b_sees

    assert result.anon_count == 0


class CollisionResult(NamedTuple):
    """Outcome of two tenants independently extracting the identical entity and fact.

    entity_content_rows: physical `entity_content` rows for the shared content-addressed id, must
        be exactly one, the dedup this design mints.
    entity_claim_rows: `entity_claim` rows staking that one content row, one per tenant.
    fact_content_rows: physical `fact_content` rows for the shared content-addressed id, must be
        exactly one.
    fact_claim_rows: `fact_claim` rows staking that one content row, one per tenant.
    a_sees_b_claim: whether A's own row-level-security session can read B's claim row, must be
        false, the isolation the content/claim split must preserve alongside the dedup.
    """

    entity_content_rows: int
    entity_claim_rows: int
    fact_content_rows: int
    fact_claim_rows: int
    a_sees_b_claim: bool


async def plant_chunk(principal: uuid.UUID) -> uuid.UUID:
    """Plant a private document and one chunk a `GraphWriter` can stamp as a fact's source.

    principal: identity that owns the document and chunk.
    """
    document, chunk = uuid.uuid4(), uuid.uuid4()
    async with acting_as(principal) as session:
        session.add(Document(id=document, content_hash=uuid.uuid4().hex, owner_id=principal))
        session.add(Chunk(id=chunk, document_id=document, ord=0, text="span", owner_id=principal))
    return chunk


async def collision_probe(principal_a: uuid.UUID, principal_b: uuid.UUID) -> CollisionResult:
    """Two independent tenants extract the identical entity and fact, both through `GraphWriter`.

    This is the bug the content/claim split fixes: before it, a content-addressed primary key
    collided across tenants, the second writer's insert either crashing outright or silently
    telling it "this content already exists", an existence side channel leaking a first tenant's
    private extraction to a second one who merely typed the same words. Here both writes must
    succeed cleanly, mint exactly one shared content row each, and never let either tenant read
    the other's own claim.

    principal_a: first tenant, extracts first.
    principal_b: second tenant, extracts the identical text independently.
    """
    migrate()
    name, type_ = "Collision Node", "Concept"
    predicate, statement = "related_to", "an identical statement both tenants extracted"
    async with async_session()() as session, session.begin():
        await session.execute(
            text("INSERT INTO principal (id) VALUES (:a), (:b)"),
            {"a": principal_a, "b": principal_b},
        )
    try:
        chunk_a = await plant_chunk(principal_a)
        chunk_b = await plant_chunk(principal_b)
        for principal, chunk in ((principal_a, chunk_a), (principal_b, chunk_b)):
            async with acting_as(principal) as session:
                writer = GraphWriter(session, principal, None)
                await writer.resolve(name, type_)
                await writer.consolidate(
                    TimedFact(subject=name, predicate=predicate, statement=statement), chunk
                )
        content_entity = entity_id(name, type_)
        content_fact = fact_id(name, predicate, "", statement)
        # the physical row counts must read past row level security entirely, an entity_claim or
        # fact_claim owned privately by A or B and never a server admin's own row or shared scope,
        # so neither the app role nor even the system (admin) principal sees them under the
        # ordinary Scoped policies; only the superuser migration connection bypasses RLS outright.
        admin = create_async_engine(settings.admin_database_url)
        try:
            async with admin.connect() as connection:
                entity_content_rows = await connection.scalar(
                    text("SELECT count(*) FROM entity_content WHERE id = :id"),
                    {"id": content_entity},
                )
                entity_claim_rows = await connection.scalar(
                    text("SELECT count(*) FROM entity_claim WHERE content_id = :id"),
                    {"id": content_entity},
                )
                fact_content_rows = await connection.scalar(
                    text("SELECT count(*) FROM fact_content WHERE id = :id"), {"id": content_fact}
                )
                fact_claim_rows = await connection.scalar(
                    text("SELECT count(*) FROM fact_claim WHERE content_id = :id"),
                    {"id": content_fact},
                )
        finally:
            await admin.dispose()
        async with acting_as(principal_a) as session:
            a_sees_b_claim = (
                await session.scalar(
                    select(FactClaim.id).where(
                        FactClaim.content_id == content_fact, FactClaim.owner_id == principal_b
                    )
                )
            ) is not None
        return CollisionResult(
            entity_content_rows=entity_content_rows or 0,
            entity_claim_rows=entity_claim_rows or 0,
            fact_content_rows=fact_content_rows or 0,
            fact_claim_rows=fact_claim_rows or 0,
            a_sees_b_claim=a_sees_b_claim,
        )
    finally:
        await purge_owner(principal_a)
        await purge_owner(principal_b)
        await drop_principals(principal_a, principal_b)


@pytest.mark.usefixtures("fake_embedder")
def test_two_tenants_extracting_identical_text_dedupe_content_and_stay_isolated(
    requires_db: None,
) -> None:
    """Two owners independently extracting identical text both succeed, no PK crash, no leak.

    One shared content row per entity and fact (the dedup), two independent claims (one per
    tenant, the union a fact can belong to A or B), and A's own row-level-security session still
    cannot read B's claim (the isolation), all three true at once, exactly the multi-tenant
    collision this design was built to kill.
    """
    result = asyncio.run(collision_probe(uuid.uuid4(), uuid.uuid4()))

    assert result.entity_content_rows == 1
    assert result.entity_claim_rows == 2
    assert result.fact_content_rows == 1
    assert result.fact_claim_rows == 2
    assert result.a_sees_b_claim is False


# an independent reconstruction of the moat's SQLAlchemy expressions, built fresh here rather than
# imported from `store.rls.predicates`, so a drive-by rewrite of the moat's predicate logic fails
# this pin first rather than silently compiling to something the source and this test both happen
# to agree on. Only `rls.compile_expression`/`rls.create_statement`/`rls.Policy` are reused, the
# neutral compiler and DDL-assembly plumbing every policy, source or test, goes through the same
# way, never the read/write/curation-admin predicate logic itself.
# bare, table-unqualified stand-ins for a policy's own target-table columns: Postgres's catalog
# drops qualification on a policy's own table (there is only ever one in scope at the top level)
# while keeping it on a correlated subquery's table (membership/groups/principal below), so these
# two shapes have to differ to match what `pg_policies.qual`/`with_check` actually re-serializes.
OWNER_ID = column("owner_id", Uuid())
SCOPE = column("scope", Uuid())
MEMBERSHIP = sa_table("membership", column("principal_id"), column("group_id"), column("role"))
GROUPS = sa_table("group_", column("id"), column("public"), column("curated"))
PRINCIPAL = sa_table("principal", column("id"), column("is_admin"))

UID = select(
    func.nullif(func.current_setting("app.uid", True), "").cast(Uuid()).label("uid")
).scalar_subquery()
LENS = select(
    func.nullif(func.current_setting("app.scope", True), "").cast(Uuid()).label("scope")
).scalar_subquery()

READ_PREDICATE = and_(
    or_(LENS.is_(None), SCOPE == LENS),
    or_(
        OWNER_ID == UID,
        SCOPE.in_(select(MEMBERSHIP.c.group_id).where(MEMBERSHIP.c.principal_id == UID)),
        SCOPE.in_(select(GROUPS.c.id).where(GROUPS.c.public)),
    ),
)
WRITE_PREDICATE = or_(
    and_(SCOPE.is_(None), OWNER_ID == UID),
    SCOPE.in_(
        select(MEMBERSHIP.c.group_id).where(
            MEMBERSHIP.c.principal_id == UID, MEMBERSHIP.c.role.in_(("writer", "admin"))
        )
    ),
)
CURATION_ADMIN_PREDICATE = and_(
    SCOPE.in_(select(GROUPS.c.id).where(GROUPS.c.curated)),
    exists(select(literal(1)).where(PRINCIPAL.c.id == UID, PRINCIPAL.c.is_admin)),
)


def create(table: str, name: str, cmd: rls.Command, using=None, check=None) -> str:
    """A `CREATE POLICY` statement compiled through the source's own neutral DDL assembly."""
    policy = rls.Policy(name, cmd, using=using, check=check)
    return rls.create_statement(table, rls.compile_policy(policy))


APPLY_CORE = [
    "ALTER TABLE document ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE document FORCE ROW LEVEL SECURITY",
    create("document", "scope_read", rls.Command.select, using=READ_PREDICATE),
    create("document", "scope_insert", rls.Command.insert, check=WRITE_PREDICATE),
    create(
        "document",
        "scope_update",
        rls.Command.update,
        using=WRITE_PREDICATE,
        check=WRITE_PREDICATE,
    ),
    create("document", "scope_delete", rls.Command.delete, using=WRITE_PREDICATE),
]
APPLY_GRANT = "GRANT SELECT, INSERT, UPDATE, DELETE ON document TO aizk_app"

DROP_CORE = [
    "DROP POLICY IF EXISTS scope_delete ON document",
    "DROP POLICY IF EXISTS scope_update ON document",
    "DROP POLICY IF EXISTS scope_insert ON document",
    "DROP POLICY IF EXISTS scope_read ON document",
    "ALTER TABLE document NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE document DISABLE ROW LEVEL SECURITY",
]
DROP_REVOKE = "REVOKE ALL ON document FROM aizk_app"


@pytest.mark.parametrize(
    ("grant", "expected"),
    [(True, [*APPLY_CORE, APPLY_GRANT]), (False, APPLY_CORE)],
)
def test_apply_statements_emit_the_canonical_force_protected_ddl(
    grant: bool, expected: list[str]
) -> None:
    """The op emits the enable, force and policy DDL, with the grant only when the role exists.

    Migration 0002 applies the policies before `aizk_app` exists, so the grant must be omitted then
    and present everywhere the role is already there.
    """
    assert rls.apply_statements("document", grant=grant) == expected


@pytest.mark.parametrize(
    ("grant", "expected"),
    [(True, [DROP_REVOKE, *DROP_CORE]), (False, DROP_CORE)],
)
def test_drop_statements_reverse_the_apply_ddl_in_order(grant: bool, expected: list[str]) -> None:
    """Drop both policies then lift force, revoking first only when the apply had granted."""
    assert rls.drop_statements("document", grant=grant) == expected


CURATION_ADMIN_APPLY = [
    create(
        "fact_claim", "curation_admin_read", rls.Command.select, using=CURATION_ADMIN_PREDICATE
    ),
    create(
        "fact_claim",
        "curation_admin_update",
        rls.Command.update,
        using=CURATION_ADMIN_PREDICATE,
        check=CURATION_ADMIN_PREDICATE,
    ),
    create(
        "fact_claim", "curation_admin_delete", rls.Command.delete, using=CURATION_ADMIN_PREDICATE
    ),
]


def test_fact_claim_declares_the_curation_admin_escape_alongside_its_scope_policies() -> None:
    """`fact_claim` extends the default scope policies with its own custom curation-admin set.

    The per-model custom-policy showcase: the first four policies `fact_claim` declares are
    exactly the inherited `Scoped` default (unaffected by the extension), and the three that
    follow are the additive server-admin escape, so a server-wide admin reaches any curated
    group's rows. The claim carries this escape rather than its content, since a curated group's
    review gate is a per-container concern, exactly what the claim, not the shared structural
    content, represents.
    """
    policies = TableBase.metadata.info["rls_policies"]["fact_claim"]
    assert [policy.name for policy in policies] == [
        "scope_read",
        "scope_insert",
        "scope_update",
        "scope_delete",
        "curation_admin_read",
        "curation_admin_update",
        "curation_admin_delete",
    ]
    compiled = [
        rls.create_statement("fact_claim", rls.compile_policy(policy)) for policy in policies[4:]
    ]
    assert compiled == CURATION_ADMIN_APPLY


def test_content_tables_declare_the_read_through_claim_immutable_policy_set() -> None:
    """Entity and fact content each carry the visible-through-a-claim, immutable policy shape.

    Three policies only, `content_read`, `content_insert`, `content_delete`, no
    `content_update` at all: content is immutable, so an UPDATE is refused outright under FORCE
    ROW LEVEL SECURITY with no permissive policy ever matching it, the database enforcing
    immutability rather than the application layer alone.
    """
    for table, claim_table in (("entity_content", "entity_claim"), ("fact_content", "fact_claim")):
        policies = TableBase.metadata.info["rls_policies"][table]
        assert [policy.name for policy in policies] == [
            "content_read",
            "content_insert",
            "content_delete",
        ]
        read, insert, delete = policies
        assert read.command == rls.Command.select
        assert insert.command == rls.Command.insert and insert.check is not None
        assert delete.command == rls.Command.delete
        compiled_read = rls.compile_expression(read.using)
        assert claim_table in compiled_read


def test_policy_matches_rejects_a_live_check_missing_where_one_is_declared() -> None:
    """A live row with no with_check at all fails a declared check clause, not a silent pass.

    Postgres auto-copies USING into WITH CHECK for the commands that need both when a migration's
    own DDL omits WITH CHECK, so this shape never arises from `apply_statements` itself; it is
    exercised directly here as the one `clause_matches` branch no live scenario naturally reaches.
    """
    compiled_using = rls.compile_expression(SCOPE.is_(None))
    policy = rls.Policy("p", rls.Command.update, using=SCOPE.is_(None), check=SCOPE.is_(None))
    assert not rls.policy_matches(policy, ("UPDATE", compiled_using, None))


def test_every_scoped_model_registers_its_table_for_the_autogenerate_guard() -> None:
    """Every RLS-declaring table, `Scoped` claims and content alike, joins the one registry.

    `Scoped.__init_subclass__` still registers a claim table early, and the mapper-construction
    hook in `store/rls/register.py` registers any `__rls_policies__`-declaring class, content
    tables included, so the guard set spans both shapes rather than only `Scoped` subclasses.
    """
    assert TableBase.metadata.info["rls"] == {
        "document",
        "chunk",
        "entity_content",
        "entity_claim",
        "fact_content",
        "fact_claim",
        "community",
        "profile",
        "session_item",
        "watermark",
    }
    assert TableBase.metadata.info["rls"] == set(TableBase.metadata.info["rls_policies"])


def test_render_emits_the_apply_scoped_rls_migration_call() -> None:
    """A diffed unprotected table renders back as an op.apply_scoped_rls line in the migration."""
    op = rls.ApplyScopedRlsOp("synthetic_unprotected")
    assert rls.render_apply_scoped_rls(None, op) == "op.apply_scoped_rls('synthetic_unprotected')"


def test_render_emits_the_drop_scoped_rls_migration_call() -> None:
    """The drop op's own renderer mirrors the apply renderer for a downgrade migration."""
    op = rls.DropScopedRlsOp("synthetic_unprotected")
    assert rls.render_drop_scoped_rls(None, op) == "op.drop_scoped_rls('synthetic_unprotected')"


SAMPLE_POLICY = rls.CompiledPolicy("scope_read", rls.Command.select, using="true")


def capture_policy_offline(policy: rls.CompiledPolicy, create: bool) -> list[str]:
    """DDL the fine-grained policy ops emit offline, the `capture_offline` counterpart for them.

    policy: the compiled policy the op carries.
    create: invoke `create_scope_policy` when true, `drop_scope_policy` when false.
    """
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    operations = Operations(context)
    if create:
        rls.CreatePolicyOp.create_scope_policy(operations, VERIFIER_PROBE, policy)
    else:
        rls.DropPolicyOp.drop_scope_policy(operations, VERIFIER_PROBE, policy)
    return [statement.strip() for statement in buffer.getvalue().split(";") if statement.strip()]


def test_create_policy_op_drops_any_same_named_policy_then_creates_it_offline() -> None:
    """create_scope_policy is idempotent: it drops any same-named policy before recreating it."""
    assert capture_policy_offline(SAMPLE_POLICY, create=True) == [
        rls.drop_statement(VERIFIER_PROBE, SAMPLE_POLICY.name),
        rls.create_statement(VERIFIER_PROBE, SAMPLE_POLICY),
    ]
    mirror = rls.CreatePolicyOp(VERIFIER_PROBE, SAMPLE_POLICY).reverse()
    assert isinstance(mirror, rls.DropPolicyOp)
    assert (mirror.table, mirror.policy) == (VERIFIER_PROBE, SAMPLE_POLICY)


def test_drop_policy_op_drops_the_named_policy_offline() -> None:
    """drop_scope_policy emits exactly one DROP POLICY IF EXISTS statement."""
    assert capture_policy_offline(SAMPLE_POLICY, create=False) == [
        rls.drop_statement(VERIFIER_PROBE, SAMPLE_POLICY.name)
    ]
    mirror = rls.DropPolicyOp(VERIFIER_PROBE, SAMPLE_POLICY).reverse()
    assert isinstance(mirror, rls.CreatePolicyOp)
    assert (mirror.table, mirror.policy) == (VERIFIER_PROBE, SAMPLE_POLICY)


def test_render_create_and_drop_policy_add_the_rls_import_and_render_the_call() -> None:
    """Both fine-grained renderers add the `rls` import a migration needs and render the call."""
    context = AutogenContext(MigrationContext.configure(dialect_name="postgresql"), MetaData())

    created = rls.render_create_policy(context, rls.CreatePolicyOp(VERIFIER_PROBE, SAMPLE_POLICY))
    assert created == (
        f"op.create_scope_policy({VERIFIER_PROBE!r}, "
        f"rls.CompiledPolicy('scope_read', rls.Command.select, 'true', None))"
    )
    dropped = rls.render_drop_policy(context, rls.DropPolicyOp(VERIFIER_PROBE, SAMPLE_POLICY))
    assert dropped == (
        f"op.drop_scope_policy({VERIFIER_PROBE!r}, "
        f"rls.CompiledPolicy('scope_read', rls.Command.select, 'true', None))"
    )
    assert "from aizk.store import rls" in context.imports


def test_render_create_policy_renders_without_a_context_to_import_into() -> None:
    """A None autogen_context (an offline `--sql` render) still renders the call, no import add."""
    op = rls.CreatePolicyOp(VERIFIER_PROBE, SAMPLE_POLICY)
    assert rls.render_create_policy(None, op) == (
        f"op.create_scope_policy({VERIFIER_PROBE!r}, "
        f"rls.CompiledPolicy('scope_read', rls.Command.select, 'true', None))"
    )


def capture_offline(table: str, apply: bool, grant: bool) -> list[str]:
    """DDL the registered alembic op emits offline when a migration invokes it.

    Drives the real `Operations` proxy in `as_sql` mode through the registered classmethods that
    `op.apply_scoped_rls`/`op.drop_scoped_rls` resolve to, so each invokes its implementation and
    the executed statements land in a buffer, exercising the invoke and implementation seam, no DB.

    table: table the op protects or unprotects.
    apply: invoke apply_scoped_rls when true, drop_scoped_rls when false.
    grant: pass through the grant flag the op forwards to the statement builder.
    """

    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    operations = Operations(context)
    if apply:
        rls.ApplyScopedRlsOp.apply_scoped_rls(operations, table, grant=grant)
    else:
        rls.DropScopedRlsOp.drop_scoped_rls(operations, table, grant=grant)
    return [statement.strip() for statement in buffer.getvalue().split(";") if statement.strip()]


@pytest.mark.parametrize("grant", [True, False])
@pytest.mark.parametrize("apply", [True, False])
def test_registered_ops_emit_and_reverse_the_canonical_ddl(apply: bool, grant: bool) -> None:
    """The alembic ops emit the builder's DDL and each reverses to its mirror op, no DB needed.

    Invoking the op offline must replay exactly `apply_statements`/`drop_statements`, and the op's
    `reverse` must hand back the opposite op carrying the same table and grant so an autogenerated
    downgrade undoes the upgrade.
    """
    builder = rls.apply_statements if apply else rls.drop_statements
    assert capture_offline("document", apply=apply, grant=grant) == builder("document", grant)

    op = (
        rls.ApplyScopedRlsOp("document", grant)
        if apply
        else rls.DropScopedRlsOp("document", grant)
    )
    mirror = op.reverse()
    expected_mirror = rls.DropScopedRlsOp if apply else rls.ApplyScopedRlsOp
    assert isinstance(mirror, expected_mirror)
    assert (mirror.table, mirror.grant) == ("document", grant)


def comparator_flags(sync: Connection, declared: dict[str, list[rls.Policy]]) -> list[str]:
    """Tables the schema comparator appends a whole-table apply op for, given a declared set.

    Runs `compare_scoped_rls` against the live catalog through a real autogen context bound to the
    connection, so the comparator body that diffs the schema and queues `ApplyScopedRlsOp` runs end
    to end rather than against a hand-faked context.

    sync: synchronous catalog connection the comparator reads through.
    declared: `table -> policies` registered in `metadata.info["rls_policies"]` for this diff.
    """
    metadata = MetaData()
    metadata.info["rls_policies"] = declared
    context = AutogenContext(MigrationContext.configure(connection=sync), metadata=metadata)
    upgrade_ops = UpgradeOps(ops=[])
    rls.compare_scoped_rls(context, upgrade_ops, set())
    return sorted(op.table for op in upgrade_ops.ops if isinstance(op, rls.ApplyScopedRlsOp))


async def diff_scoped_rls(synthetic: str) -> list[str]:
    """Tables the comparator would flag, given the live schema plus one synthetic scoped table.

    synthetic: a scoped table name with no migration, which must come back as needing protection.
    """
    migrate()
    declared = dict(TableBase.metadata.info["rls_policies"])
    declared[synthetic] = TableBase.metadata.info["rls_policies"]["document"]
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.connect() as connection:
            return await connection.run_sync(lambda sync: comparator_flags(sync, declared))
    finally:
        await admin.dispose()


def test_autogenerate_flags_only_the_unprotected_scoped_table(requires_db: None) -> None:
    """A synthetic scoped table with no migration is flagged while the migrated tables are not."""
    flagged = asyncio.run(diff_scoped_rls("synthetic_unprotected"))

    assert flagged == ["synthetic_unprotected"]
    assert "document" not in flagged


def fine_grained_ops(
    sync: Connection, declared: dict[str, list[rls.Policy]]
) -> list[tuple[str, str]]:
    """`(op class name, table)` pairs the comparator queues for an already force-and-enabled table.

    sync: synchronous catalog connection the comparator reads through.
    declared: `table -> policies` registered in `metadata.info["rls_policies"]` for this diff.
    """
    metadata = MetaData()
    metadata.info["rls_policies"] = declared
    context = AutogenContext(MigrationContext.configure(connection=sync), metadata=metadata)
    upgrade_ops = UpgradeOps(ops=[])
    rls.compare_scoped_rls(context, upgrade_ops, set())
    return [
        (type(op).__name__, op.table)
        for op in upgrade_ops.ops
        if isinstance(op, rls.CreatePolicyOp | rls.DropPolicyOp)
    ]


async def diff_one_drifted_policy() -> list[tuple[str, str]]:
    """Ops the comparator queues after one live policy is dropped from an otherwise-intact table.

    Proves the differ is real: an already force-and-enabled table with one missing policy gets a
    single targeted `CreatePolicyOp`, never the whole-table `ApplyScopedRlsOp` bootstrap, so a
    small drift costs a small, precisely-scoped fix.
    """
    migrate()
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.begin() as connection:
            await connection.execute(text("DROP POLICY scope_read ON document"))
        try:
            async with admin.connect() as connection:
                return await connection.run_sync(
                    lambda sync: fine_grained_ops(
                        sync, dict(TableBase.metadata.info["rls_policies"])
                    )
                )
        finally:
            async with admin.begin() as connection:
                await connection.execute(text(rls.apply_statements("document")[2]))
    finally:
        await admin.dispose()


def test_autogenerate_diffs_one_drifted_policy_without_a_whole_table_reapply(
    requires_db: None,
) -> None:
    """Dropping just scope_read queues one targeted CreatePolicyOp, not a whole-table bootstrap."""
    assert asyncio.run(diff_one_drifted_policy()) == [("CreatePolicyOp", "document")]


EXTRA_POLICY = "CREATE POLICY scope_extra ON document FOR SELECT USING (true)"


async def diff_one_stale_policy() -> list[tuple[str, str]]:
    """Ops the comparator queues when a live policy exists that the model no longer declares.

    The mirror of `diff_one_drifted_policy`: a policy present in the catalog but absent from the
    declared set is dropped on its own, `DropPolicyOp`, rather than folded into a whole-table
    reapply.
    """
    migrate()
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.begin() as connection:
            await connection.execute(text(EXTRA_POLICY))
        try:
            async with admin.connect() as connection:
                return await connection.run_sync(
                    lambda sync: fine_grained_ops(
                        sync, dict(TableBase.metadata.info["rls_policies"])
                    )
                )
        finally:
            async with admin.begin() as connection:
                await connection.execute(text("DROP POLICY IF EXISTS scope_extra ON document"))
    finally:
        await admin.dispose()


def test_autogenerate_drops_one_live_policy_no_longer_declared(requires_db: None) -> None:
    """A live policy absent from the declared set is queued for its own targeted drop."""
    assert asyncio.run(diff_one_stale_policy()) == [("DropPolicyOp", "document")]


async def comparator_ops_with_nothing_declared() -> list[MigrateOperation]:
    """Ops the comparator queues against a real connection whose metadata declares nothing."""
    migrate()
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.connect() as connection:

            def run(sync: Connection) -> list[MigrateOperation]:
                context = AutogenContext(MigrationContext.configure(connection=sync), MetaData())
                upgrade_ops = UpgradeOps(ops=[])
                rls.compare_scoped_rls(context, upgrade_ops, set())
                return upgrade_ops.ops

            return await connection.run_sync(run)
    finally:
        await admin.dispose()


def test_comparator_no_ops_when_nothing_is_declared(requires_db: None) -> None:
    """A live connection with an empty declared registry still queues nothing, never an error."""
    assert asyncio.run(comparator_ops_with_nothing_declared()) == []


def test_comparator_no_ops_without_a_connection() -> None:
    """The comparator returns early and queues nothing when autogenerate runs offline.

    An offline migration context carries no connection, so the catalog cannot be read and the
    comparator must skip rather than diff against nothing, the guard that keeps `--sql` runs safe.
    """
    context = AutogenContext(MigrationContext.configure(dialect_name="postgresql"), MetaData())
    ups = UpgradeOps(ops=[])
    rls.compare_scoped_rls(context, ups, set())
    assert ups.ops == []


PROBE_ROLE = "aizk_force_probe"

# a scope_read policy mirroring the production owner branch, enough to show force decides whether
# the table owner is subject to it without pulling in the membership join.
PROBE_POLICY = "owner_id = (SELECT current_setting('app.uid', true)::uuid)"


def probe_dsn() -> str:
    """The admin DSN rewritten to log in as the non-superuser probe role."""
    parts = urlsplit(settings.admin_database_url)
    netloc = f"{PROBE_ROLE}:{PROBE_ROLE}@{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def setup_force_probe(owner: uuid.UUID, row_id: uuid.UUID) -> None:
    """Create a non-superuser-owned table with one row and a scope policy, force not yet set.

    owner: principal the planted row belongs to, never the principal the probe later acts as.
    row_id: id of the planted row.
    """
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.begin() as connection:
            await connection.execute(text("INSERT INTO principal (id) VALUES (:o)"), {"o": owner})
            await connection.execute(
                text(
                    "DO $$ BEGIN IF NOT EXISTS "
                    f"(SELECT FROM pg_roles WHERE rolname = '{PROBE_ROLE}') THEN "
                    f"CREATE ROLE {PROBE_ROLE} LOGIN PASSWORD '{PROBE_ROLE}' "
                    "NOSUPERUSER NOBYPASSRLS; END IF; END $$;"
                )
            )
            await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {PROBE_ROLE}"))
            await connection.execute(
                text("CREATE TABLE force_probe (id uuid PRIMARY KEY, owner_id uuid NOT NULL)")
            )
            await connection.execute(text(f"ALTER TABLE force_probe OWNER TO {PROBE_ROLE}"))
            await connection.execute(text("ALTER TABLE force_probe ENABLE ROW LEVEL SECURITY"))
            await connection.execute(
                text(f"CREATE POLICY scope_read ON force_probe FOR SELECT USING ({PROBE_POLICY})")
            )
            await connection.execute(
                text("INSERT INTO force_probe (id, owner_id) VALUES (:id, :o)"),
                {"id": row_id, "o": owner},
            )
    finally:
        await admin.dispose()


async def teardown_force_probe(owner: uuid.UUID) -> None:
    """Drop the probe table, role and planted principal so the run leaves nothing behind.

    owner: principal whose planted row and identity to remove.
    """
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.begin() as connection:
            # drop owned by removes the probe-owned table and revokes its grants in one step, so
            # the role drops cleanly and the block no-ops when a prior run already cleared it.
            await connection.execute(
                text(
                    "DO $$ BEGIN IF EXISTS "
                    f"(SELECT FROM pg_roles WHERE rolname = '{PROBE_ROLE}') THEN "
                    f"DROP OWNED BY {PROBE_ROLE} CASCADE; DROP ROLE {PROBE_ROLE}; END IF; END $$;"
                )
            )
            await connection.execute(text("DELETE FROM principal WHERE id = :o"), {"o": owner})
    finally:
        await admin.dispose()


def count_as_owner(sync: Connection, actor: uuid.UUID) -> int:
    """Rows the probe role sees in its own table while acting as `actor` under the current policy.

    sync: synchronous connection authenticated as the table-owning probe role.
    actor: principal app.uid is set to, who does not own the planted row.
    """
    sync.execute(text("SELECT set_config('app.uid', :uid, true)"), {"uid": str(actor)})
    return sync.execute(text("SELECT count(*) FROM force_probe")).scalar_one()


async def force_owner_leak() -> tuple[int, int]:
    """Count the probe row as its owner role with force off then on, acting as a foreign principal.

    Without force the table owner bypasses row level security and sees the foreign row, so the
    count is one. Turning force on subjects the owner to the policy and the foreign row disappears.
    """
    owner, actor, row_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    migrate()
    await teardown_force_probe(owner)
    await setup_force_probe(owner, row_id)
    probe = create_async_engine(probe_dsn())
    try:
        async with probe.begin() as connection:
            leaked = await connection.run_sync(count_as_owner, actor)
            await connection.execute(text("ALTER TABLE force_probe FORCE ROW LEVEL SECURITY"))
            forced = await connection.run_sync(count_as_owner, actor)
            return leaked, forced
    finally:
        await probe.dispose()
        await teardown_force_probe(owner)


def test_removing_force_makes_the_table_owner_leak(requires_db: None) -> None:
    """Force is load-bearing: without it the owning role reads a row the scope policy must hide."""
    leaked, forced = asyncio.run(force_owner_leak())

    assert leaked == 1
    assert forced == 0


def test_engine_and_session_are_cached_singletons() -> None:
    """The shared session factory, and the engine it binds, are built once per DSN, not rebuilt."""
    assert async_session() is async_session()
    assert async_session().kw["bind"] is async_session().kw["bind"]


async def scoped_read_without_context() -> None:
    """Run an ORM read of a scoped table in a session opened without `acting_as`."""
    migrate()
    async with async_session()() as session, session.begin():
        await session.execute(select(Document))


def test_scoped_query_without_acting_as_raises(requires_db: None) -> None:
    """A scoped ORM read that forgot `acting_as` fails loud instead of silently running open."""
    with pytest.raises(NoTenantContext):
        asyncio.run(scoped_read_without_context())


async def verify_live() -> list[str]:
    """Run the no-leak verifier over the live schema's registered scoped tables."""
    migrate()
    expected = set(TableBase.metadata.info["rls"])
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.connect() as connection:
            return await connection.run_sync(lambda sync: rls.verify_scoped_rls(sync, expected))
    finally:
        await admin.dispose()


def test_check_rls_passes_on_the_live_schema(requires_db: None) -> None:
    """Every migrated scoped table forces row security with the canonical scope policies."""
    assert asyncio.run(verify_live()) == []


# a synthetic scoped table protected by the canonical apply DDL, used to isolate one regression at
# a time so the verifier can be shown to flag exactly the clause that was undone. Built against
# bare, unqualified columns (`default_scope_policies` takes any `ColumnElement`, table-bound or
# not) rather than a mapped model, so the probe stays a plain hand-created table with no ORM class
# of its own, and `verify_scoped_rls`'s own `declared=` parameter lets the test hand it this
# probe's policies directly instead of requiring them registered on the real metadata.
VERIFIER_PROBE = "verifier_probe"
PROBE_POLICIES = rls.default_scope_policies(column("owner_id", Uuid()), column("scope", Uuid()))
PROBE_SCOPE_READ_USING = PROBE_POLICIES[0].using
PROBE_SCOPE_INSERT_CHECK = PROBE_POLICIES[1].check
assert PROBE_SCOPE_READ_USING is not None
assert PROBE_SCOPE_INSERT_CHECK is not None
PROBE_READ = rls.compile_expression(PROBE_SCOPE_READ_USING)
PROBE_WRITE = rls.compile_expression(PROBE_SCOPE_INSERT_CHECK)


def probe_apply_statements() -> list[str]:
    """The enable, force, and policy DDL that protects `VERIFIER_PROBE`, canonical shape."""
    return [
        f"ALTER TABLE {VERIFIER_PROBE} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {VERIFIER_PROBE} FORCE ROW LEVEL SECURITY",
        *(
            rls.create_statement(VERIFIER_PROBE, rls.compile_policy(policy))
            for policy in PROBE_POLICIES
        ),
    ]


# each regression undoes one part of the canonical protection on a freshly protected probe table,
# mapping a label to the DDL that breaks it and the single reason the verifier must then report.
# Together they walk every negative branch `verify_scoped_rls` checks, so one parametrized property
# replaces a separate example test per branch while still asserting the exact reason string for
# each distinct violating state.
REGRESSIONS: dict[str, tuple[list[str], list[str]]] = {
    "clean": ([], []),
    "force_removed": (
        [f"ALTER TABLE {VERIFIER_PROBE} NO FORCE ROW LEVEL SECURITY"],
        [f"{VERIFIER_PROBE}: row level security not forced"],
    ),
    "disabled": (
        [f"ALTER TABLE {VERIFIER_PROBE} DISABLE ROW LEVEL SECURITY"],
        [f"{VERIFIER_PROBE}: row level security not enabled"],
    ),
    "missing_read": (
        [f"DROP POLICY scope_read ON {VERIFIER_PROBE}"],
        [f"{VERIFIER_PROBE}: missing scope_read policy"],
    ),
    "missing_insert": (
        [f"DROP POLICY scope_insert ON {VERIFIER_PROBE}"],
        [f"{VERIFIER_PROBE}: missing scope_insert policy"],
    ),
    "missing_update": (
        [f"DROP POLICY scope_update ON {VERIFIER_PROBE}"],
        [f"{VERIFIER_PROBE}: missing scope_update policy"],
    ),
    "missing_delete": (
        [f"DROP POLICY scope_delete ON {VERIFIER_PROBE}"],
        [f"{VERIFIER_PROBE}: missing scope_delete policy"],
    ),
    "wrong_cmd": (
        [
            f"DROP POLICY scope_read ON {VERIFIER_PROBE}",
            f"CREATE POLICY scope_read ON {VERIFIER_PROBE} FOR ALL USING ({PROBE_READ})",
        ],
        [f"{VERIFIER_PROBE}: scope_read guards 'ALL', expected 'SELECT'"],
    ),
    "bad_using": (
        [
            f"DROP POLICY scope_read ON {VERIFIER_PROBE}",
            f"CREATE POLICY scope_read ON {VERIFIER_PROBE} FOR SELECT USING (true)",
        ],
        [f"{VERIFIER_PROBE}: scope_read clause does not scope correctly"],
    ),
    "widened_read": (
        [
            f"DROP POLICY scope_read ON {VERIFIER_PROBE}",
            f"CREATE POLICY scope_read ON {VERIFIER_PROBE} FOR SELECT USING ({PROBE_WRITE})",
        ],
        [f"{VERIFIER_PROBE}: scope_read clause does not scope correctly"],
    ),
    "bad_check": (
        [
            f"DROP POLICY scope_update ON {VERIFIER_PROBE}",
            f"CREATE POLICY scope_update ON {VERIFIER_PROBE} FOR UPDATE "
            f"USING ({PROBE_WRITE}) WITH CHECK (true)",
        ],
        [f"{VERIFIER_PROBE}: scope_update clause does not scope correctly"],
    ),
    "role_dropped": (
        [
            f"DROP POLICY scope_insert ON {VERIFIER_PROBE}",
            f"CREATE POLICY scope_insert ON {VERIFIER_PROBE} FOR INSERT WITH CHECK ({PROBE_READ})",
        ],
        [f"{VERIFIER_PROBE}: scope_insert clause does not scope correctly"],
    ),
}


async def verifier_probe_violations(regression: str) -> list[str]:
    """Protect a synthetic scoped table with the canonical DDL, undo one part, then verify.

    regression: the `REGRESSIONS` label whose breaking DDL runs after the canonical apply.
    """
    migrate()
    admin = create_async_engine(settings.admin_database_url)
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f"DROP TABLE IF EXISTS {VERIFIER_PROBE}"))
            await connection.execute(
                text(
                    f"CREATE TABLE {VERIFIER_PROBE} "
                    "(id uuid PRIMARY KEY, owner_id uuid NOT NULL, scope uuid)"
                )
            )
            for statement in probe_apply_statements():
                await connection.execute(text(statement))
            for statement in REGRESSIONS[regression][0]:
                await connection.execute(text(statement))
        async with admin.connect() as connection:
            return await connection.run_sync(
                lambda sync: rls.verify_scoped_rls(
                    sync, {VERIFIER_PROBE}, declared={VERIFIER_PROBE: PROBE_POLICIES}
                )
            )
    finally:
        async with admin.begin() as connection:
            await connection.execute(text(f"DROP TABLE IF EXISTS {VERIFIER_PROBE}"))
        await admin.dispose()


@pytest.mark.parametrize("regression", list(REGRESSIONS))
def test_verifier_catches_each_canonical_regression(requires_db: None, regression: str) -> None:
    """The verifier passes a canonically protected table and names the exact undone clause.

    The clean case proves the apply DDL satisfies the no-leak contract. Every other case undoes one
    guarantee, force, enablement, a missing policy, a wrong command, or a clause that stopped
    scoping, and the verifier must report that one reason and no other, so removing FORCE stays an
    explicit, asserting case of the most dangerous undo.
    """
    assert asyncio.run(verifier_probe_violations(regression)) == REGRESSIONS[regression][1]
