# live_fact stops being a security barrier so the planner can inline it into recall and
# push vector-distance ordering down to the content indexes. Row security on fact_claim
# still fences every scan through the security-invoker view, and the view's own qualifiers
# only hide temporal states of rows the caller may already read.

from collections.abc import Sequence

from alembic import op

revision: str = "0005_inline_live_fact"
down_revision: str | None = "0004_drop_tsvector_lane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER VIEW live_fact RESET (security_barrier)")


def downgrade() -> None:
    op.execute("ALTER VIEW live_fact SET (security_barrier = true)")
