# Widen the stored health reading into the keyed operator reading table.
# Revision ID 0011_operator_snapshot

from alembic import op

revision: str = "0011_operator_snapshot"
down_revision: str | None = "0010_health_snapshot"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Rename the table now that it holds more than one kind of reading.

    Health was the first measurement the console could not take for itself, and it will not
    be the last. The queue diagnosis and the platform usage aggregate need the same database
    owner for the same reason, so the row already keyed by which reading it holds now carries
    all three, and the name says so. The single existing row keeps the key `latest`, which no
    reader asks for anymore, so it is renamed in place rather than dropped.
    """
    op.rename_table("health_snapshot", "operator_snapshot")
    op.execute("UPDATE operator_snapshot SET key = 'health' WHERE key = 'latest'")


def downgrade() -> None:
    """Return to the single health reading, dropping what the older schema cannot key."""
    op.execute("DELETE FROM operator_snapshot WHERE key <> 'health'")
    op.execute("UPDATE operator_snapshot SET key = 'latest' WHERE key = 'health'")
    op.rename_table("operator_snapshot", "health_snapshot")
