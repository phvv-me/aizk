"""Rename the user table's `zitadel_sub` column to the vendor-neutral `oidc_subject`.

The identity-provider subject a token maps to is a generic OIDC claim, not a Zitadel-specific
one, so the column loses its vendor name as the verifier becomes pluggable across any OIDC
provider (Logto, a customer's Okta, GitHub). A plain column rename, so the unique index and every
row's value ride along untouched, and an existing database keeps its linked identities. The class
still maps to the physical `principal` table, which `user` being a reserved word keeps unrenamed.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_oidc_subject"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("principal", "zitadel_sub", new_column_name="oidc_subject")


def downgrade() -> None:
    op.alter_column("principal", "oidc_subject", new_column_name="zitadel_sub")
