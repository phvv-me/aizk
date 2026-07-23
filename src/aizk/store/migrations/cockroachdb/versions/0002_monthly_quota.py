# Add atomic monthly cost quota counters.
# Revision ID 0002_monthly_quota

import sqlalchemy as sa

from alembic import op

revision: str = "0002_monthly_quota"
down_revision: str | None = "0001_cockroachdb"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the open coordination table used before expensive model work."""
    op.create_table(
        "monthly_quota_counter",
        sa.Column("subject_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("period", sa.Date(), primary_key=True, nullable=False),
        sa.Column("kind", sa.String(length=16), primary_key=True, nullable=False),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("used >= 0", name="ck_monthly_quota_counter_used_nonnegative"),
    )


def downgrade() -> None:
    """Remove monthly counters after quotas are disabled."""
    op.drop_table("monthly_quota_counter")
