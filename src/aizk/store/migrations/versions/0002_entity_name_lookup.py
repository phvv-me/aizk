# Functional index for query-mention entity seeding, the exact lowercased name match the
# personalized PageRank expansion starts from.
# Revision ID 0002_entity_name_lookup

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_entity_name_lookup"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_entity_content_name_lower"


def upgrade() -> None:
    op.create_index(INDEX, "entity_content", [sa.text("lower(name)")])


def downgrade() -> None:
    op.drop_index(INDEX, table_name="entity_content")
