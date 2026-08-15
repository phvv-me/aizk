# Add the owner-only queue of obsolete object-store layouts.
# Revision ID 0003_object_retirement

import rls
import sqlalchemy as sa

from aizk.config import settings
from aizk.store import ObjectRetirement
from alembic import op

revision: str = "0003_object_retirement"
down_revision: str | None = "0002_policy_names"
branch_labels: str | None = None
depends_on: str | None = None


def _retirement_rls() -> rls.RLSState:
    """Compile the owner-only retirement policy frozen into this revision."""
    return rls.RLSState.declared((rls.Policy.select(sa.false(), roles=(settings.app_role,)),))


def upgrade() -> None:
    """Create the durable retirement queue and deny the application role every row."""
    connection = op.get_bind()
    ObjectRetirement.__table__.create(connection)
    state = _retirement_rls()
    for statement in rls.apply_statements(ObjectRetirement.__table__, state):
        connection.execute(statement)


def downgrade() -> None:
    """Drop pending retirement records while leaving their stored bytes untouched."""
    connection = op.get_bind()
    state = _retirement_rls()
    for statement in rls.drop_statements(ObjectRetirement.__table__, state):
        connection.execute(statement)
    ObjectRetirement.__table__.drop(connection)
