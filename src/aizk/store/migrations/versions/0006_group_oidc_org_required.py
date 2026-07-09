"""Require every group to mirror a Logto organization: drop the null-org locals, then NOT NULL.

Groups now exist only as projections of Logto organizations, minted by `User.sync_groups` when a
member's token first names the org. There is no hand-created local group any more, so a group
carrying no `oidc_org_id` is a relic of the removed manual-create path. Deleting it fires the
`group_demote_scopes` trigger, so its shared rows fall back to their owners' private scope rather
than dangling, and the column then takes its NOT NULL constraint.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_group_oidc_org_required"
down_revision: str | None = "0005_simplify_sharing_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM group_ WHERE oidc_org_id IS NULL")
    op.alter_column("group_", "oidc_org_id", nullable=False)


def downgrade() -> None:
    op.alter_column("group_", "oidc_org_id", nullable=True)
