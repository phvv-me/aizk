# Make temporal and vector storage portable across PostgreSQL-compatible databases.
# Revision ID 0003_portable_database

import sqlalchemy as sa
from pgvector.sqlalchemy import HALFVEC, VECTOR
from sqlalchemy.dialects.postgresql import TSTZRANGE
from sqlmodel import select

from aizk.config import settings
from aizk.store.ddl import CreateView, DropView
from alembic import op

revision: str = "0003_portable_database"
down_revision: str | None = "0002_durable_usage"
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


def live_fact_select(portable: bool) -> sa.Select:
    """Build the live view against either side of this migration."""
    temporal = (
        ("valid_from", "valid_to", "recorded_from", "recorded_to")
        if portable
        else ("valid", "recorded")
    )
    claim = sa.table(
        "fact_claim",
        *(
            sa.column(name)
            for name in (
                "id",
                "content_id",
                "created_by",
                "scopes",
                *temporal,
                "last_accessed",
                "access_count",
                "attributes",
                "perspective_key",
                "source_chunk_id",
                "promoted_from",
            )
        ),
    )
    content = sa.table(
        "fact_content",
        *(
            sa.column(name)
            for name in ("id", "subject_id", "object_id", "predicate", "statement", "embedding")
        ),
    )
    columns = (
        claim.c.id,
        claim.c.content_id,
        content.c.subject_id,
        content.c.object_id,
        content.c.predicate,
        content.c.statement,
        content.c.embedding,
        claim.c.created_by,
        claim.c.scopes,
        *(claim.c[name] for name in temporal),
        claim.c.last_accessed,
        claim.c.access_count,
        claim.c.attributes,
        claim.c.perspective_key,
        claim.c.source_chunk_id,
        claim.c.promoted_from,
    )
    current = (
        sa.and_(
            claim.c.recorded_to.is_(None),
            sa.or_(claim.c.valid_from.is_(None), claim.c.valid_from <= sa.func.now()),
            sa.or_(claim.c.valid_to.is_(None), claim.c.valid_to > sa.func.now()),
        )
        if portable
        else sa.and_(
            sa.func.upper_inf(claim.c.recorded),
            sa.or_(claim.c.valid.is_(None), claim.c.valid.op("@>")(sa.func.now())),
        )
    )
    return (
        select(columns[0], columns[1], columns[2], columns[3])
        .add_columns(*columns[4:])
        .select_from(claim.join(content, content.c.id == claim.c.content_id))
        .where(current)
    )


def vector_index(name: str, table: str, operator: str) -> str:
    """Render one PostgreSQL ANN index using the deployment's selected method."""
    return f"CREATE INDEX {name} ON {table} USING {settings.index_backend} (embedding {operator})"


def upgrade() -> None:
    """Replace PostgreSQL-only ranges and half vectors with portable columns."""
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

    for name in (
        "uq_fact_claim_live",
        "ix_fact_claim_live",
        "ix_fact_claim_recorded",
        "ix_fact_claim_valid",
    ):
        op.drop_index(name, table_name="fact_claim")
    op.add_column("fact_claim", sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True))
    op.add_column("fact_claim", sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "fact_claim", sa.Column("recorded_from", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "fact_claim", sa.Column("recorded_to", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        """
        UPDATE fact_claim
        SET valid_from = CASE WHEN isempty(valid) THEN now() ELSE lower(valid) END,
            valid_to = CASE WHEN isempty(valid) THEN now() ELSE upper(valid) END,
            recorded_from = lower(recorded),
            recorded_to = upper(recorded)
        """
    )
    op.alter_column(
        "fact_claim",
        "recorded_from",
        nullable=False,
        server_default=sa.func.now(),
    )
    op.drop_column("fact_claim", "valid")
    op.drop_column("fact_claim", "recorded")
    op.create_index("ix_fact_claim_valid", "fact_claim", ["valid_from", "valid_to"])
    op.create_index("ix_fact_claim_recorded", "fact_claim", ["recorded_from", "recorded_to"])
    op.create_index(
        "ix_fact_claim_live",
        "fact_claim",
        ["valid_from", "valid_to"],
        postgresql_where=sa.text("recorded_to IS NULL"),
    )
    op.create_index(
        "uq_fact_claim_live",
        "fact_claim",
        ["content_id", "scopes", "perspective_key"],
        unique=True,
        postgresql_where=sa.text("recorded_to IS NULL"),
    )
    op.execute(
        CreateView(
            live_fact_select(portable=True),
            "live_fact",
            postgresql_with={"security_invoker": True},
        )
    )


def downgrade() -> None:
    """Restore the original PostgreSQL ranges and half vectors."""
    op.execute(DropView(sa.table("live_fact"), if_exists=True))
    for name in (
        "uq_fact_claim_live",
        "ix_fact_claim_live",
        "ix_fact_claim_recorded",
        "ix_fact_claim_valid",
    ):
        op.drop_index(name, table_name="fact_claim")
    op.add_column("fact_claim", sa.Column("valid", TSTZRANGE(), nullable=True))
    op.add_column("fact_claim", sa.Column("recorded", TSTZRANGE(), nullable=True))
    op.execute(
        """
        UPDATE fact_claim
        SET valid = CASE
                WHEN valid_from IS NULL AND valid_to IS NULL THEN NULL
                ELSE tstzrange(valid_from, valid_to, '[)')
            END,
            recorded = tstzrange(recorded_from, recorded_to, '[)')
        """
    )
    op.alter_column(
        "fact_claim",
        "recorded",
        nullable=False,
        server_default=sa.text("tstzrange(now(), NULL, '[)')"),
    )
    for name in ("valid_from", "valid_to", "recorded_from", "recorded_to"):
        op.drop_column("fact_claim", name)
    op.create_index("ix_fact_claim_valid", "fact_claim", ["valid"], postgresql_using="gist")
    op.create_index("ix_fact_claim_recorded", "fact_claim", ["recorded"], postgresql_using="gist")
    op.create_index(
        "ix_fact_claim_live",
        "fact_claim",
        ["valid"],
        postgresql_using="gist",
        postgresql_where=sa.text("upper_inf(recorded)"),
    )
    op.create_index(
        "uq_fact_claim_live",
        "fact_claim",
        ["content_id", "scopes", "perspective_key"],
        unique=True,
        postgresql_where=sa.text("upper_inf(recorded)"),
    )

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
            live_fact_select(portable=False),
            "live_fact",
            postgresql_with={"security_invoker": True},
        )
    )
