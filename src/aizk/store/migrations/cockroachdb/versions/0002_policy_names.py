# Move row security to typed settings and command-derived policy names.
# Revision ID 0002_policy_names

from aizk.config import settings
from aizk.store.migrations.policy_context import PolicyContextTransition
from alembic import op

revision: str = "0002_policy_names"
down_revision: str | None = "0001_cockroachdb"
branch_labels: str | None = None
depends_on: str | None = None

_TRANSITION = PolicyContextTransition(settings.app_role, cockroachdb=True)


def upgrade() -> None:
    """Adopt typed settings and command-derived policy names."""
    _TRANSITION.replace(op.execute, legacy=False)


def downgrade() -> None:
    """Restore the application name carrier and semantic policy names."""
    _TRANSITION.replace(op.execute, legacy=True)
