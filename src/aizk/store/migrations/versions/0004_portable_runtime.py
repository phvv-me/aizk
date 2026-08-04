# Add backend-neutral coordination, queue, history, and schedule tables.
# Revision ID 0004_portable_runtime

import sqlalchemy as sa

from alembic import op

revision: str = "0004_portable_runtime"
down_revision: str | None = "0003_portable_database"
branch_labels: str | None = None
depends_on: str | None = None


def timestamps() -> tuple[sa.Column, sa.Column]:
    """Build the common creation and update columns used by portable runtime tables."""
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    """Create transaction locks and the portable worker's durable state."""
    op.create_table(
        "coordination_lock",
        sa.Column("key", sa.Text(), primary_key=True, nullable=False),
    )
    op.create_table(
        "queue_task",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        *timestamps(),
        sa.Column("entrypoint", sa.Text(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_queue_task_entrypoint", "queue_task", ["entrypoint"])
    op.create_index(
        "ix_queue_task_pick",
        "queue_task",
        ["status", "priority", "created_at"],
    )
    op.create_index(
        "uq_queue_task_active_dedupe",
        "queue_task",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text(
            "dedupe_key IS NOT NULL AND status IN ('queued', 'picked', 'failed')"
        ),
    )
    op.create_table(
        "queue_event",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        *timestamps(),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("entrypoint", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    for column in ("task_id", "entrypoint", "status"):
        op.create_index(f"ix_queue_event_{column}", "queue_event", [column])
    op.create_table(
        "queue_schedule",
        sa.Column("name", sa.Text(), primary_key=True, nullable=False),
        *timestamps(),
        sa.Column("expression", sa.Text(), nullable=False),
        sa.Column("next_run", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_queue_schedule_next_run", "queue_schedule", ["next_run"])


def downgrade() -> None:
    """Remove portable runtime state after draining its jobs."""
    for table in ("queue_schedule", "queue_event", "queue_task", "coordination_lock"):
        op.drop_table(table)
