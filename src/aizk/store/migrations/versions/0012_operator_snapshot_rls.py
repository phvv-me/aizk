# Let row security, not one endpoint's check, decide who reads the operator readings.
# Revision ID 0012_operator_snapshot_rls

import rls
import sqlalchemy as sa
from rls.alembic import AlterRLSOp

from aizk.config import settings
from alembic import op

revision: str = "0012_operator_snapshot_rls"
down_revision: str | None = "0011_operator_snapshot"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "operator_snapshot"
# The table was created without row security at all, which is the state this revision
# moves away from and the one its downgrade restores.
_OPEN = rls.RLSState(enabled=False, forced=False, policies=())


def _operator_rls() -> rls.RLSState:
    """Compile the operator-standing policy frozen into this revision."""
    standing = rls.current_setting("operator", sa.Boolean(), prefix="app")
    return rls.RLSState.declared(
        (
            rls.Policy.select(
                "operator_snapshot_read",
                standing,
                roles=(settings.app_role,),
            ),
        )
    )


def upgrade() -> None:
    """Refuse these readings to a caller the database cannot see holds operator standing.

    The table was created open because nothing but the operator console reads it, and the
    console checks the role itself. That left the guarantee resting on one `if` in one
    process, while the readings grew to carry material the rest of the schema protects with
    row security, including the names of artifacts whose own table is scoped. The caller's
    operator standing already travels into the transaction beside its scopes, so the
    decision moves to where every other one is made. An unset setting reads as null and
    denies, so a caller that never proved operator standing sees nothing.

    Writes stay unpoliced because every pass is written under the owner role, which is where
    the credential to measure these readings lives.
    """
    op.invoke(AlterRLSOp(_TABLE, before=_OPEN, after=_operator_rls()))


def downgrade() -> None:
    """Return the table to the open state the console alone was trusted to guard."""
    op.invoke(AlterRLSOp(_TABLE, before=_operator_rls(), after=_OPEN))
