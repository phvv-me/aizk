# Store embeddings as half vectors on PostgreSQL, keeping full vectors on CockroachDB.
# Revision ID 0008_halfvec_storage

import sqlalchemy as sa
from pgvector.sqlalchemy import HALFVEC, VECTOR

from aizk.config import DatabaseBackend, settings
from aizk.store.ddl import CreateView, DropView
from aizk.store.models.views.live_fact import LiveFact
from alembic import op

revision: str = "0008_halfvec_storage"
down_revision: str | None = "0007_web_egress"
branch_labels: str | None = None
depends_on: str | None = None

_VECTOR_TABLES = (
    "chunk",
    "entity_kind",
    "entity_content",
    "fact_content",
    "community",
    "profile",
    "session_item",
)
_VECTOR_INDEX_TABLES = tuple(table for table in _VECTOR_TABLES if table != "entity_kind")


def vector_index(name: str, table: str, operator: str) -> str:
    """Render one PostgreSQL ANN index using the deployment's selected method."""
    return f"CREATE INDEX {name} ON {table} USING {settings.index_backend} (embedding {operator})"


def upgrade() -> None:
    """Halve embedding bytes and index size where halfvec exists, a no-op on CockroachDB."""
    if settings.database_backend is DatabaseBackend.cockroachdb:
        return
    op.execute(DropView(sa.table("live_fact"), if_exists=True))
    for table in _VECTOR_INDEX_TABLES:
        op.drop_index(f"ix_{table}_embedding", table_name=table)
    for table in _VECTOR_TABLES:
        op.alter_column(
            table,
            "embedding",
            type_=HALFVEC(settings.embed_dim),
            postgresql_using=f"embedding::halfvec({settings.embed_dim})",
        )
    for table in _VECTOR_INDEX_TABLES:
        op.execute(vector_index(f"ix_{table}_embedding", table, "halfvec_cosine_ops"))
    op.execute(
        CreateView(
            LiveFact.__view_select__(),
            "live_fact",
            postgresql_with={"security_invoker": True},
        )
    )


def downgrade() -> None:
    """Return to full vector storage, the portable column both backends share."""
    if settings.database_backend is DatabaseBackend.cockroachdb:
        return
    op.execute(DropView(sa.table("live_fact"), if_exists=True))
    for table in _VECTOR_INDEX_TABLES:
        op.drop_index(f"ix_{table}_embedding", table_name=table)
    for table in _VECTOR_TABLES:
        op.alter_column(
            table,
            "embedding",
            type_=VECTOR(settings.embed_dim),
            postgresql_using=f"embedding::vector({settings.embed_dim})",
        )
    for table in _VECTOR_INDEX_TABLES:
        op.execute(vector_index(f"ix_{table}_embedding", table, "vector_cosine_ops"))
    op.execute(
        CreateView(
            LiveFact.__view_select__(),
            "live_fact",
            postgresql_with={"security_invoker": True},
        )
    )
