# Add the durable single-use upload capability store shared by every AIZK process.
# Revision ID 0003_upload_capability

from collections.abc import Sequence

import rls
import sqlalchemy as sa
from rls.alembic import AlterRLSOp
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import select

from alembic import op

revision: str = "0003_upload_capability"
down_revision: str | None = "0002_artifacts_usage"
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


def upload_capability_rls() -> rls.RLSState:
    """Compile the scope lattice frozen into this migration for the capability store."""
    table = sa.table("upload_capability", sa.column("scopes", ARRAY(sa.Uuid())))
    scopes = table.c.scopes
    standing = rls.current_setting("scopes", JSONB(), prefix="app")
    readable = _scope_authority(standing, "read")
    writable = _scope_authority(standing, "write")
    public = _scope_authority(standing, "public")
    nonempty = sa.func.cardinality(scopes) > 0
    read = sa.and_(
        nonempty,
        sa.or_(
            scopes.op("<@")(readable),
            sa.and_(sa.func.cardinality(scopes) == 1, scopes.op("<@")(public)),
        ),
    )
    write = sa.and_(nonempty, scopes.op("<@")(writable), sa.true())
    return rls.RLSState.declared(
        (
            rls.Policy.select("scope_read", read, roles=(_APP_ROLE,)),
            rls.Policy.insert("scope_insert", write, roles=(_APP_ROLE,)),
            rls.Policy.delete("scope_delete", write, roles=(_APP_ROLE,)),
        )
    )


def upgrade() -> None:
    op.create_table(
        "upload_capability",
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
        sa.Column("capability", sa.String(128), nullable=False),
        sa.Column("ticket", JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("capability"),
    )
    op.create_index("ix_upload_capability_created_by", "upload_capability", ["created_by"])
    op.create_index("ix_upload_capability_expires_at", "upload_capability", ["expires_at"])
    op.create_index(
        "ix_upload_capability_scopes",
        "upload_capability",
        ["scopes"],
        postgresql_using="gin",
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE upload_capability TO {_APP_ROLE}")
    op.invoke(AlterRLSOp("upload_capability", before=None, after=upload_capability_rls()))


def downgrade() -> None:
    op.drop_table("upload_capability")
