# Move row security to typed settings and command-derived policy names.
# Revision ID 0015_policy_names

from aizk.store.migrations.policy_context import PolicyContextTransition
from alembic import op

revision: str = "0015_policy_names"
down_revision: str | None = "0014_caption_metadata"
branch_labels: str | None = None
depends_on: str | None = None

_TRANSITION = PolicyContextTransition("aizk_app", cockroachdb=False)


def upgrade() -> None:
    """Adopt typed settings and command-derived policy names."""
    _TRANSITION.replace(op.execute, legacy=False)


def downgrade() -> None:
    """Restore the JSON setting and semantic policy names."""
    _TRANSITION.replace(op.execute, legacy=True)
