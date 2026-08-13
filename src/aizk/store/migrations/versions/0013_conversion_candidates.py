# Keep conversion policy provenance and rejected candidates beside each immutable original.
# Revision ID 0013_conversion_candidates

import sqlalchemy as sa

from alembic import op

revision: str = "0013_conversion_candidates"
down_revision: str | None = "0012_operator_snapshot_rls"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the durable cursor and quarantine used by safe reconversion passes."""
    op.add_column("artifact_content", sa.Column("conversion_policy", sa.Text(), nullable=True))
    op.add_column("artifact_content", sa.Column("candidate_markdown", sa.Text(), nullable=True))
    op.add_column("artifact_content", sa.Column("candidate_policy", sa.Text(), nullable=True))
    op.add_column("artifact_content", sa.Column("candidate_error", sa.Text(), nullable=True))
    op.create_index(
        "ix_artifact_content_conversion_policy",
        "artifact_content",
        ["conversion_policy"],
    )
    op.create_index(
        "ix_artifact_content_candidate_policy",
        "artifact_content",
        ["candidate_policy"],
    )


def downgrade() -> None:
    """Remove conversion provenance and quarantined candidate derivatives."""
    op.drop_index("ix_artifact_content_candidate_policy", table_name="artifact_content")
    op.drop_index("ix_artifact_content_conversion_policy", table_name="artifact_content")
    op.drop_column("artifact_content", "candidate_error")
    op.drop_column("artifact_content", "candidate_policy")
    op.drop_column("artifact_content", "candidate_markdown")
    op.drop_column("artifact_content", "conversion_policy")
