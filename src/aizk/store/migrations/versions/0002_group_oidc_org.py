"""Mirror each sharing group onto its Logto organization.

Adds `group_.oidc_org_id`, the stable Logto organization id a group is the local projection of, so
membership and roles can be sourced from a verified token's claims rather than hand-managed in
aizk. Purely additive: the column is nullable (a group minted before the delegation, or one with no
Logto counterpart, simply carries none) and unique (one local group per organization), and no row
level security policy references it, so every existing scope policy and the whole intersection
lattice keep compiling and enforcing exactly as before.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_group_oidc_org"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("group_", sa.Column("oidc_org_id", sa.Text(), nullable=True))
    op.create_unique_constraint("group__oidc_org_id_key", "group_", ["oidc_org_id"])


def downgrade() -> None:
    op.drop_constraint("group__oidc_org_id_key", "group_", type_="unique")
    op.drop_column("group_", "oidc_org_id")
