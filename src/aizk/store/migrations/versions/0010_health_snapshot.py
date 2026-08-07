# Store the operator health report the owner measures so the public API can read it.
# Revision ID 0010_health_snapshot

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010_health_snapshot"
down_revision: str | None = "0009_unreadable_conversions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the single row a worker writes and the operator console reads.

    Four of the probes behind the health report count rows and read policies across every
    scope, which only the database owner may do. The process serving the console runs as the
    application role and is deliberately denied the owner credential, so it asked for a
    reading it could never take and answered every request with a failed authentication. The
    measurement now happens where the credential legitimately lives and lands here, and
    `updated_at` is what lets the console say a reading is old rather than present it as now.
    """
    op.create_table(
        "health_snapshot",
        sa.Column("key", sa.Text(), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "report",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    """Drop the stored reading, which every pass reproduces."""
    op.drop_table("health_snapshot")
