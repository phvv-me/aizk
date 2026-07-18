# Add immutable artifacts, object metadata, and usage accounting.
# Revision ID 0002_artifacts_usage

from collections.abc import Sequence

import rls
import sqlalchemy as sa
from rls.alembic import AlterRLSOp
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB
from sqlmodel import select

from alembic import op

revision: str = "0002_artifacts_usage"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "aizk_app"


def _scope_authority(standing: sa.ColumnElement, permission: str) -> sa.ColumnElement:
    """Turn one JSON scope permission into a PostgreSQL UUID array."""
    values = (
        sa.func.jsonb_array_elements_text(standing.op("->")(permission))
        .table_valued("value")
        .render_derived()
    )
    return sa.func.array(select(values.c.value.cast(sa.Uuid())).scalar_subquery())


def scoped_rls(
    table_name: str,
    *,
    mutable: bool,
    read_through: str | None = None,
    deletable: bool = False,
) -> rls.RLSState:
    """Compile the scope lattice frozen into this migration."""
    table = sa.table(
        table_name,
        sa.column("scopes", ARRAY(sa.Uuid())),
        *(sa.column(f"{read_through}_id", sa.Uuid()),) if read_through else (),
    )
    scopes = table.c.scopes
    standing = rls.current_setting("scopes", JSONB(), prefix="app")
    writable = _scope_authority(standing, "write")
    nonempty = sa.func.cardinality(scopes) > 0
    if read_through:
        parent = sa.table(
            read_through,
            sa.column("id", sa.Uuid()),
            sa.column("scopes", ARRAY(sa.Uuid())),
        )
        parent_id = table.c[f"{read_through}_id"]
        read = parent_id.in_(select(parent.c.id))
        parent_scope = sa.tuple_(parent_id, scopes).in_(select(parent.c.id, parent.c.scopes))
    else:
        readable = _scope_authority(standing, "read")
        public = _scope_authority(standing, "public")
        read = sa.and_(
            nonempty,
            sa.or_(
                scopes.op("<@")(readable),
                sa.and_(sa.func.cardinality(scopes) == 1, scopes.op("<@")(public)),
            ),
        )
        parent_scope = sa.true()
    write = sa.and_(nonempty, scopes.op("<@")(writable), parent_scope)
    policies = [
        rls.Policy.select("scope_read", read, roles=(_APP_ROLE,)),
        rls.Policy.insert("scope_insert", write, roles=(_APP_ROLE,)),
    ]
    if mutable:
        policies.append(rls.Policy.update("scope_update", write, write, roles=(_APP_ROLE,)))
    if deletable:
        policies.append(rls.Policy.delete("scope_delete", write, roles=(_APP_ROLE,)))
    return rls.RLSState.declared(tuple(policies))


def blob_rls() -> rls.RLSState:
    """Expose object metadata only through visible artifact revisions."""
    blob = sa.table("blob", sa.column("id", sa.Uuid()))
    content = sa.table("artifact_content", sa.column("blob_id", sa.Uuid()))
    return rls.RLSState.declared(
        (
            rls.Policy.select(
                "blob_read",
                blob.c.id.in_(select(content.c.blob_id)),
                roles=(_APP_ROLE,),
            ),
            rls.Policy.insert("blob_insert", sa.true(), roles=(_APP_ROLE,)),
        )
    )


def upgrade() -> None:
    op.create_table(
        "blob",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.Uuid(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("stored_size", sa.Integer(), nullable=False),
        sa.Column(
            "encoding",
            ENUM("identity", "zstd", name="blob_encoding"),
            server_default="identity",
            nullable=False,
        ),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("storage_version", sa.String(512), nullable=True),
        sa.Column("media_type", sa.String(255), nullable=True),
        sa.Column("etag", sa.String(512), nullable=True),
        sa.Column("integrity_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("integrity_error", sa.String(1024), nullable=True),
        sa.CheckConstraint("size >= 0", name="ck_blob_size_nonnegative"),
        sa.CheckConstraint("stored_size >= 0", name="ck_blob_stored_size_nonnegative"),
        sa.CheckConstraint("stored_size <= size", name="ck_blob_stored_size_bounded"),
        sa.CheckConstraint("storage_key <> ''", name="ck_blob_storage_key_nonempty"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_blob_content_hash_size", "blob", ["content_hash", "size"])
    op.create_index("ix_blob_integrity_checked_at", "blob", ["integrity_checked_at"])

    op.create_table(
        "artifact",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "scopes",
            ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("promoted_from", sa.Uuid(), nullable=True),
        sa.CheckConstraint("name <> ''", name="ck_artifact_name_nonempty"),
        sa.ForeignKeyConstraint(["promoted_from"], ["artifact.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "promoted_from",
            "scopes",
            name="uq_artifact_promotion_scope",
        ),
        sa.UniqueConstraint("source_uri", "scopes", name="uq_artifact_source_scope"),
    )
    op.create_index("ix_artifact_created_by", "artifact", ["created_by"])
    op.create_index("ix_artifact_promoted_from", "artifact", ["promoted_from"])
    op.create_index(
        "ix_artifact_scopes",
        "artifact",
        ["scopes"],
        postgresql_using="gin",
    )

    op.create_table(
        "artifact_content",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "scopes",
            ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "state",
            ENUM(
                "pending",
                "queued",
                "processing",
                "ready",
                "failed",
                name="artifact_content_state",
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("companion_text", sa.Text(), nullable=True),
        sa.Column("markdown", sa.Text(), nullable=True),
        sa.Column("docling_json", JSONB(), nullable=True),
        sa.Column("details", JSONB(), server_default="{}", nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_artifact_content_revision_positive",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifact.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["blob_id"], ["blob.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "artifact_id",
            "revision",
            name="uq_artifact_content_revision",
        ),
    )
    op.create_index(
        "ix_artifact_content_artifact_id",
        "artifact_content",
        ["artifact_id"],
    )
    op.create_index("ix_artifact_content_blob_id", "artifact_content", ["blob_id"])
    op.create_index(
        "ix_artifact_content_created_by",
        "artifact_content",
        ["created_by"],
    )
    op.create_index(
        "ix_artifact_content_scopes",
        "artifact_content",
        ["scopes"],
        postgresql_using="gin",
    )
    op.create_index("ix_artifact_content_state", "artifact_content", ["state"])

    op.create_table(
        "usage_event",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "scopes",
            ARRAY(sa.Uuid()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "operation",
            ENUM(
                "recall",
                "remember_text",
                "remember_file",
                "share",
                "artifact_read",
                name="usage_event_operation",
            ),
            nullable=False,
        ),
        sa.Column("targets", ARRAY(sa.Uuid()), nullable=False),
        sa.Column("request_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("response_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items", sa.Integer(), server_default="1", nullable=False),
        sa.Column("duration_ms", sa.Float(), server_default="0.0", nullable=False),
        sa.CheckConstraint(
            "request_bytes >= 0",
            name="ck_usage_request_bytes_nonnegative",
        ),
        sa.CheckConstraint(
            "response_bytes >= 0",
            name="ck_usage_response_bytes_nonnegative",
        ),
        sa.CheckConstraint("items >= 0", name="ck_usage_items_nonnegative"),
        sa.CheckConstraint(
            "duration_ms >= 0",
            name="ck_usage_duration_ms_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_usage_event_created_by", "usage_event", ["created_by"])
    op.create_index("ix_usage_event_operation", "usage_event", ["operation"])
    op.create_index(
        "ix_usage_event_scopes",
        "usage_event",
        ["scopes"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_usage_event_targets",
        "usage_event",
        ["targets"],
        postgresql_using="gin",
    )

    op.add_column("document", sa.Column("artifact_id", sa.Uuid(), nullable=True))
    op.add_column(
        "document",
        sa.Column("artifact_content_id", sa.Uuid(), nullable=True),
    )
    op.create_index("ix_document_artifact_id", "document", ["artifact_id"])
    op.create_index(
        "ix_document_artifact_content_id",
        "document",
        ["artifact_content_id"],
    )
    op.create_foreign_key(
        "fk_document_artifact_id_artifact",
        "document",
        "artifact",
        ["artifact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_document_artifact_content_id_artifact_content",
        "document",
        "artifact_content",
        ["artifact_content_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column(
        "document",
        "title",
        existing_type=sa.String(),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "document",
        "source_uri",
        existing_type=sa.String(),
        type_=sa.Text(),
        existing_nullable=True,
    )

    for table in ("artifact", "artifact_content", "blob", "usage_event"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {_APP_ROLE}")

    op.invoke(
        AlterRLSOp(
            "artifact",
            before=None,
            after=scoped_rls("artifact", mutable=True),
        )
    )
    op.invoke(
        AlterRLSOp(
            "artifact_content",
            before=None,
            after=scoped_rls(
                "artifact_content",
                mutable=True,
                read_through="artifact",
            ),
        )
    )
    op.invoke(AlterRLSOp("blob", before=None, after=blob_rls()))
    op.invoke(
        AlterRLSOp(
            "usage_event",
            before=None,
            after=scoped_rls("usage_event", mutable=False),
        )
    )

    for policy in ("scope_read", "scope_insert", "scope_update", "scope_delete"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON chunk")
    op.invoke(
        AlterRLSOp(
            "chunk",
            before=None,
            after=scoped_rls(
                "chunk",
                mutable=True,
                deletable=True,
                read_through="document",
            ),
        )
    )


def downgrade() -> None:
    raise NotImplementedError("artifact storage has no lossless reverse migration")
