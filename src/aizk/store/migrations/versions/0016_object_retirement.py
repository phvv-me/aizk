# Defer obsolete object deletion until every authorized stale reader has expired.
# Revision ID 0016_object_retirement

import rls
import sqlalchemy as sa
from rls.alembic import AlterRLSOp

from aizk.config import settings
from alembic import op

revision: str = "0016_object_retirement"
down_revision: str | None = "0015_policy_names"
branch_labels: str | None = None
depends_on: str | None = None


def _retirement_rls() -> rls.RLSState:
    """Compile the owner-only retirement policy frozen into this revision."""
    return rls.RLSState.declared((rls.Policy.select(sa.false(), roles=(settings.app_role,)),))


def upgrade() -> None:
    """Add the durable queue of immutable layouts awaiting safe deletion."""
    op.create_table(
        "object_retirement",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("storage_version", sa.String(512), nullable=True),
        sa.Column("stored_size", sa.Integer(), nullable=False),
        sa.Column("delete_after", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "stored_size >= 0",
            name="ck_object_retirement_stored_size_nonnegative",
        ),
        sa.CheckConstraint(
            "storage_key <> ''",
            name="ck_object_retirement_storage_key_nonempty",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index(
        "ix_object_retirement_delete_after",
        "object_retirement",
        ["delete_after"],
    )
    op.invoke(
        AlterRLSOp(
            "object_retirement",
            before=None,
            after=_retirement_rls(),
        )
    )


def downgrade() -> None:
    """Drop pending retirement records while leaving their stored bytes untouched."""
    op.invoke(
        AlterRLSOp(
            "object_retirement",
            before=_retirement_rls(),
            after=None,
        )
    )
    op.drop_index("ix_object_retirement_delete_after", table_name="object_retirement")
    op.drop_table("object_retirement")
