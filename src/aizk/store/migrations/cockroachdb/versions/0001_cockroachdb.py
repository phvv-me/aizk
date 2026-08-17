# Build the native CockroachDB schema from AIZK's portable mapped metadata.
# Revision ID 0001_cockroachdb

from importlib import import_module
from typing import Protocol, cast

import rls
from inflection import underscore
from sqlalchemy import Table

from aizk.config import settings
from aizk.store import Fact, TableBase
from aizk.store.ddl import CreateView, DropView
from aizk.store.migrations.cockroachdb import scoped_cspann
from alembic import op

revision: str = "0001_cockroachdb"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


class OntologySeeds(Protocol):
    """The frozen ontology values shared with the PostgreSQL base revision."""

    ENTITY_KINDS: tuple[tuple[str, str, str, bool], ...]
    RELATION_KINDS: tuple[tuple[str, str, str, bool], ...]
    _RELATION_POLICIES: dict[str, str]


_seeds = cast(
    "OntologySeeds",
    import_module("aizk.store.migrations.versions.0001_init"),
)
_INCREMENTAL_TABLES = frozenset({"object_retirement"})


def mapped_tables() -> tuple[Table, ...]:
    """Return the frozen baseline tables in dependency order, excluding later revisions."""
    return tuple(
        table
        for table in TableBase.metadata.sorted_tables
        if not table.info.get("is_view") and table.name not in _INCREMENTAL_TABLES
    )


def _legacy_rls(table: Table, state: rls.RLSState) -> rls.RLSState:
    """Keep the original policy names frozen into this baseline revision."""
    if table.name == "operator_snapshot":
        prefixes = {"select": "operator_snapshot"}
    elif table.name == "blob":
        prefixes = {"select": "blob", "insert": "blob"}
    elif table.name in {"entity_content", "fact_content"}:
        prefixes = {"select": "content", "insert": "content"}
    else:
        prefixes = {command.value: "scope" for command in rls.Command}
    suffixes = {"select": "read"}
    policies = tuple(
        policy.model_copy(
            update={
                "name": f"{prefixes[policy.command.value]}_"
                f"{suffixes.get(policy.command.value, policy.command.value)}"
            }
        )
        for policy in state.policies
    )
    return state.model_copy(update={"policies": policies})


def _optimize_storage(tables: tuple[Table, ...]) -> tuple[str, ...]:
    """Isolate large embeddings and remove source ANN indexes replaced by C-SPANN."""
    embedded = tuple(table.name for table in tables if "embedding" in table.c)
    for table in embedded:
        op.execute(f'ALTER TABLE "{table}" SET (schema_locked=false)')
        op.execute(f'DROP INDEX IF EXISTS "{table}"@"ix_{table}_embedding"')
        op.execute(
            f'ALTER TABLE "{table}" ADD COLUMN embedding_isolated '
            f"VECTOR({settings.embed_dim}) CREATE FAMILY embedding_data"
        )
        op.execute(f'ALTER TABLE "{table}" DROP COLUMN embedding')
        op.execute(f'ALTER TABLE "{table}" RENAME COLUMN embedding_isolated TO embedding')
    return embedded


def _enforce_parent_scope_identity() -> tuple[str, ...]:
    """Make child scope authorization equivalent to parent visibility."""
    tables = ("artifact", "artifact_content", "document", "chunk")
    for table in tables:
        op.execute(f'ALTER TABLE "{table}" SET (schema_locked=false)')
    for statement in (
        "ALTER TABLE artifact ADD CONSTRAINT uq_artifact_id_scopes UNIQUE (id, scopes)",
        "ALTER TABLE artifact_content ADD CONSTRAINT fk_artifact_content_scope "
        "FOREIGN KEY (artifact_id, scopes) REFERENCES artifact (id, scopes) "
        "ON DELETE CASCADE ON UPDATE CASCADE",
        "ALTER TABLE document ADD CONSTRAINT uq_document_id_scopes UNIQUE (id, scopes)",
        "ALTER TABLE chunk ADD CONSTRAINT fk_chunk_document_scope "
        "FOREIGN KEY (document_id, scopes) REFERENCES document (id, scopes) "
        "ON DELETE CASCADE ON UPDATE CASCADE",
    ):
        op.execute(statement)
    return tables


def _lock_schema(tables: tuple[str, ...]) -> None:
    """Restore CockroachDB schema locks after baseline construction."""
    for table in sorted(set(tables)):
        op.execute(f'ALTER TABLE "{table}" SET (schema_locked=true)')


def _grant_app_role(app_role: str) -> None:
    """Give the runtime role durable access to the public application schema."""
    for statement in (
        f"GRANT USAGE ON SCHEMA public TO {app_role}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {app_role}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {app_role}",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app_role}",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {app_role}",
    ):
        op.execute(statement)


def _revoke_app_role(app_role: str) -> None:
    """Reverse the baseline grants before dropping its public objects."""
    for statement in (
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {app_role}",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE USAGE, SELECT ON SEQUENCES FROM {app_role}",
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM {app_role}",
        f"REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public FROM {app_role}",
        f"REVOKE USAGE ON SCHEMA public FROM {app_role}",
    ):
        op.execute(statement)


def upgrade() -> None:
    """Create the frozen CockroachDB baseline from its mapped table definitions."""
    connection = op.get_bind()
    tables = mapped_tables()
    TableBase.metadata.create_all(connection, tables=tables, checkfirst=False)
    embedded = _optimize_storage(tables)
    scoped_children = _enforce_parent_scope_identity()
    op.bulk_insert(
        TableBase.metadata.tables["entity_kind"],
        [
            {
                "name": underscore(name),
                "description": description,
                "domain": domain,
                "structural": structural,
            }
            for name, description, domain, structural in _seeds.ENTITY_KINDS
        ],
    )
    op.bulk_insert(
        TableBase.metadata.tables["relation_kind"],
        [
            {
                "name": underscore(name),
                "description": description,
                "domain": domain,
                "structural": structural,
                "policy": _seeds._RELATION_POLICIES.get(underscore(name), "set"),
            }
            for name, description, domain, structural in _seeds.RELATION_KINDS
        ],
    )
    functions = {
        "aizk_blob_visible": ("artifact_content", "blob_id"),
        "aizk_entity_content_visible": ("entity_claim", "content_id"),
        "aizk_fact_content_visible": ("fact_claim", "content_id"),
    }
    for name, (source_table, field) in functions.items():
        op.execute(
            f"CREATE FUNCTION {name}(target UUID) RETURNS BOOL "
            f"LANGUAGE SQL STABLE SECURITY INVOKER AS $$ "
            f"SELECT EXISTS (SELECT 1 FROM {source_table} WHERE {field} = target) $$"
        )
    for parent in ("artifact", "document"):
        op.execute(
            f"CREATE FUNCTION aizk_{parent}_visible(target UUID, target_scopes UUID[]) "
            f"RETURNS BOOL LANGUAGE SQL STABLE SECURITY INVOKER AS $$ "
            f"SELECT EXISTS (SELECT 1 FROM {parent} "
            f"WHERE id = target AND scopes = target_scopes) $$"
        )
    op.execute(
        "CREATE INDEX ix_chunk_fts ON chunk USING GIN "
        "(to_tsvector('english', coalesce(lexical, text)))"
    )
    catalog = TableBase.metadata.info.get("rls")
    if not isinstance(catalog, rls.Catalog):
        raise RuntimeError("mapped metadata has no RLS catalog")
    for table in catalog.protected:
        if table not in tables:
            continue
        state = catalog.state(table)
        if state is None:
            raise RuntimeError(f"protected table {table.name} has no RLS state")
        for statement in rls.apply_statements(table, _legacy_rls(table, state)):
            connection.execute(statement)
    op.execute(
        CreateView(
            Fact.Live.__view_select__(),
            "live_fact",
            postgresql_with={"security_invoker": True},
        )
    )
    app_role = connection.dialect.identifier_preparer.quote(settings.app_role)
    scoped_cspann.create()
    _grant_app_role(app_role)
    _lock_schema((*embedded, *scoped_children))


def downgrade() -> None:
    """Drop the CockroachDB view and mapped schema."""
    connection = op.get_bind()
    app_role = connection.dialect.identifier_preparer.quote(settings.app_role)
    _revoke_app_role(app_role)
    scoped_cspann.drop()
    op.execute(DropView(Fact.Live.__table__, if_exists=True))
    catalog = TableBase.metadata.info.get("rls")
    if not isinstance(catalog, rls.Catalog):
        raise RuntimeError("mapped metadata has no RLS catalog")
    tables = mapped_tables()
    for table in catalog.protected:
        if table not in tables:
            continue
        state = catalog.state(table)
        if state is None:
            raise RuntimeError(f"protected table {table.name} has no RLS state")
        for statement in rls.drop_statements(table, _legacy_rls(table, state)):
            connection.execute(statement)
    for function in (
        "aizk_document_visible(UUID, UUID[])",
        "aizk_artifact_visible(UUID, UUID[])",
        "aizk_fact_content_visible(UUID)",
        "aizk_entity_content_visible(UUID)",
        "aizk_blob_visible(UUID)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
    TableBase.metadata.drop_all(
        connection,
        tables=tuple(reversed(tables)),
        checkfirst=False,
    )
