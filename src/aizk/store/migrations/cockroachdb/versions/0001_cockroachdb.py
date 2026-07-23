# Build the native CockroachDB schema from AIZK's portable mapped metadata.
# Revision ID 0001_cockroachdb

from importlib import import_module
from typing import Protocol, cast

import rls
from inflection import underscore
from sqlalchemy import Table

from aizk.store import Fact, TableBase
from aizk.store.ddl import CreateView, DropView
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


def mapped_tables() -> tuple[Table, ...]:
    """Return mapped base tables in dependency order, excluding mapped views."""
    return tuple(
        table
        for table in TableBase.metadata.sorted_tables
        if not table.info.get("is_view") and table.name != "monthly_quota_counter"
    )


def upgrade() -> None:
    """Create portable tables, C-SPANN indexes, full-text search, views, and RLS."""
    connection = op.get_bind()
    tables = mapped_tables()
    TableBase.metadata.create_all(connection, tables=tables, checkfirst=False)
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
    for name, (table, field) in functions.items():
        op.execute(
            f"CREATE FUNCTION {name}(target UUID) RETURNS BOOL "
            f"LANGUAGE SQL STABLE SECURITY INVOKER AS $$ "
            f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {field} = target) $$"
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
    catalog.create_all(connection)
    op.execute(
        CreateView(
            Fact.Live.__view_select__(),
            "live_fact",
            postgresql_with={"security_invoker": True},
        )
    )


def downgrade() -> None:
    """Drop the CockroachDB view and mapped schema."""
    connection = op.get_bind()
    op.execute(DropView(Fact.Live.__table__, if_exists=True))
    TableBase.metadata.drop_all(
        connection,
        tables=tuple(reversed(mapped_tables())),
        checkfirst=False,
    )
