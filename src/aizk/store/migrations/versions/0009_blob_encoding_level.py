# Record the compression policy each stored object was laid out under.
# Revision ID 0009_blob_encoding_level

import sqlalchemy as sa

from alembic import op

revision: str = "0009_blob_encoding_level"
down_revision: str | None = "0008_halfvec_storage"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the level marker that tells compaction which objects predate the current policy.

    Every existing object starts null, which reads as "laid out under an unknown policy" and
    is exactly right. A deployment that has been running since before the store recorded a
    level cannot know what its objects were compressed at, so the first `aizk admin storage
    compact` pass re-evaluates all of them and stamps what it decided.
    """
    op.add_column("blob", sa.Column("encoding_level", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_blob_encoding_level_range",
        "blob",
        "encoding_level IS NULL OR encoding_level BETWEEN 1 AND 22",
    )
    op.create_index("ix_blob_encoding_level", "blob", ["encoding_level"])


def downgrade() -> None:
    """Drop the level marker, leaving every stored object exactly where it sits."""
    op.drop_index("ix_blob_encoding_level", table_name="blob")
    op.drop_constraint("ck_blob_encoding_level_range", "blob", type_="check")
    op.drop_column("blob", "encoding_level")
