# Give a conversion Docling's own policy check will never pass a state retry stops offering,
# on CockroachDB.
# Revision ID 0005_unreadable_conversions

from importlib import import_module
from typing import Protocol, cast

from alembic import op

revision: str = "0005_unreadable_conversions"
down_revision: str | None = "0004_storage_footprint"
branch_labels: str | None = None
depends_on: str | None = None


class UnreadableConversions(Protocol):
    """The names the PostgreSQL revision of the same change already settled."""

    STATE: str
    VALUE: str


# CockroachDB runs its own migration branch, so the PostgreSQL revision would never reach a
# CockroachDB deployment. The names belong to one revision, so they are imported rather than
# restated, exactly as this branch's earlier revisions import from their twins.
_shared = cast(
    "UnreadableConversions",
    import_module("aizk.store.migrations.versions.0009_unreadable_conversions"),
)


def upgrade() -> None:
    """Add the `unreadable` value beside `failed` on the durable conversion state.

    CockroachDB's `ALTER TYPE ... ADD VALUE` is the same statement as the PostgreSQL twin's,
    since both dialects took the syntax from the same enum extension. It still runs outside a
    transaction on this backend the way every enum change here does, so the tolerant `IF NOT
    EXISTS` form is what keeps a repeated run safe.
    """
    op.execute(f"ALTER TYPE {_shared.STATE} ADD VALUE IF NOT EXISTS '{_shared.VALUE}'")


def downgrade() -> None:
    """Fold `unreadable` rows back to `failed` and leave the enum value in the catalog.

    CockroachDB cannot drop an enum value either, so the honest downgrade here is the same
    one the PostgreSQL twin makes: reconcile the data for an older mapping rather than
    pretend the type itself can be restored.
    """
    op.execute(f"UPDATE artifact_content SET state = 'failed' WHERE state = '{_shared.VALUE}'")
