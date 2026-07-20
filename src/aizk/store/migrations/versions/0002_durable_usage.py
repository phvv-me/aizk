# Make transport accounting idempotent across worker restarts.
# Revision ID 0002_durable_usage

import sqlalchemy as sa

from alembic import op

revision: str = "0002_durable_usage"
down_revision: str | None = "0001_init"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the stable transport capture identity to the immutable usage ledger."""
    op.add_column("usage_event", sa.Column("capture_key", sa.String(length=64), nullable=True))
    op.execute("UPDATE usage_event SET capture_key = 'legacy:' || id::text")
    op.alter_column("usage_event", "capture_key", nullable=False)
    op.create_index("uq_usage_event_capture_key", "usage_event", ["capture_key"], unique=True)


def downgrade() -> None:
    """Remove the transport capture identity."""
    op.drop_index("uq_usage_event_capture_key", table_name="usage_event")
    op.drop_column("usage_event", "capture_key")
