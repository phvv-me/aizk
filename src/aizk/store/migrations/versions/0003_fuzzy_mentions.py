# Trigram support for fuzzy query-mention entity seeding: the pg_trgm extension and a
# trigram index over the lowercased entity name.
# Revision ID 0003_fuzzy_mentions

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_fuzzy_mentions"
down_revision: str | None = "0002_entity_name_lookup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_entity_content_name_trgm"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        INDEX,
        "entity_content",
        [sa.text("lower(name) gin_trgm_ops")],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="entity_content")
