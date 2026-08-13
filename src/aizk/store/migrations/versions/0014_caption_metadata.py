# Store exact image-caption routing provenance beside the promoted derivative.
# Revision ID 0014_caption_metadata

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014_caption_metadata"
down_revision: str | None = "0013_conversion_candidates"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add durable per-figure model, provider, latency, usage, and cost metadata."""
    op.add_column(
        "artifact_content",
        sa.Column(
            "caption_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Remove stored image-caption routing provenance."""
    op.drop_column("artifact_content", "caption_metadata")
