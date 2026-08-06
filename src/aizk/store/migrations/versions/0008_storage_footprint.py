# Shrink what the store keeps on disk and record how each piece was laid out.
# Revision ID 0008_storage_footprint

import sqlalchemy as sa
from pgvector.sqlalchemy import HALFVEC, VECTOR
from sqlalchemy.dialects.postgresql import JSONB

from aizk.config import DatabaseBackend, settings
from aizk.store.ddl import CreateView, DropView
from aizk.store.models.views.live_fact import LiveFact
from alembic import op

revision: str = "0008_storage_footprint"
down_revision: str | None = "0007_web_egress"
branch_labels: str | None = None
depends_on: str | None = None

# Each unread artifact derivative and the value that blanks it before it is dropped.
# `details` is NOT NULL, so its empty object stands in for the null the nullable
# `docling_json` takes.
DERIVATIVES: dict[str, str] = {"docling_json": "NULL", "details": "'{}'::jsonb"}

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


def retype_embeddings(column: HALFVEC | VECTOR, cast: str) -> None:
    """Rewrite every embedding column and its ANN index to one pgvector type.

    column: the mapped type each `embedding` column takes.
    cast: the pgvector type name the rewrite casts through, which also names the cosine
        operator class the rebuilt indexes use.
    """
    # CockroachDB has no half precision vector type, so that backend keeps the portable full
    # `VECTOR` column both branches share and there is nothing here to convert.
    if settings.database_backend is DatabaseBackend.cockroachdb:
        return
    # A view pins the column types of the relations it selects, so `live_fact` has to go
    # before the fact embedding changes type and be rebuilt against the new one afterwards.
    op.execute(DropView(sa.table("live_fact"), if_exists=True))
    for table in _VECTOR_INDEX_TABLES:
        op.drop_index(f"ix_{table}_embedding", table_name=table)
    for table in _VECTOR_TABLES:
        op.alter_column(
            table,
            "embedding",
            type_=column,
            postgresql_using=f"embedding::{cast}({settings.embed_dim})",
        )
    for table in _VECTOR_INDEX_TABLES:
        op.execute(vector_index(f"ix_{table}_embedding", table, f"{cast}_cosine_ops"))
    op.execute(
        CreateView(
            LiveFact.__view_select__(),
            "live_fact",
            postgresql_with={"security_invoker": True},
        )
    )


def upgrade() -> None:
    """Halve embedding bytes, mark each object's compression policy, and drop the unread
    artifact derivatives."""
    retype_embeddings(HALFVEC(settings.embed_dim), "halfvec")
    # An object starts null, which reads as laid out under an unknown policy and is exactly
    # right. A deployment running since before the store recorded a level cannot know what
    # its objects were compressed at, so the first `aizk admin storage compact` pass
    # re-evaluates all of them and stamps what it decided. A default would erase that.
    op.add_column("blob", sa.Column("encoding_level", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_blob_encoding_level_range",
        "blob",
        "encoding_level IS NULL OR encoding_level BETWEEN 1 AND 22",
    )
    op.create_index("ix_blob_encoding_level", "blob", ["encoding_level"])
    # Dropping a column is a catalog edit. PostgreSQL marks the attribute dead and stops
    # returning it, but every existing row keeps the value on disk and every out-of-line
    # value keeps its rows in the TOAST relation, so a bare `DROP COLUMN` reclaims nothing.
    # Blanking first rewrites each row without the value and releases the TOAST chunks,
    # which is what turns the drop into space, so this `UPDATE` has to precede the drop.
    blanked = ", ".join(f"{name} = {value}" for name, value in DERIVATIVES.items())
    op.execute(f"UPDATE artifact_content SET {blanked}")
    for name in DERIVATIVES:
        op.drop_column("artifact_content", name)
    # `indexed_at` replaces them and is what the re-chunk sweep orders on. Every existing
    # revision starts null, which reads as indexed under an unknown chunking policy and puts
    # it at the front of the first `aizk admin data rechunk` pass.
    op.add_column("artifact_content", sa.Column("indexed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    """Restore the derivative columns empty, drop the level marker, and return to full
    vectors."""
    op.drop_column("artifact_content", "indexed_at")
    op.add_column(
        "artifact_content",
        sa.Column("details", JSONB(), server_default="{}", nullable=False),
    )
    op.add_column("artifact_content", sa.Column("docling_json", JSONB(), nullable=True))
    op.drop_index("ix_blob_encoding_level", table_name="blob")
    op.drop_constraint("ck_blob_encoding_level_range", "blob", type_="check")
    op.drop_column("blob", "encoding_level")
    retype_embeddings(VECTOR(settings.embed_dim), "vector")
