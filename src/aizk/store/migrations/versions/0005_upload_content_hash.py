# Purge legacy upload capabilities that are not bound to a declared content hash.
# Revision ID 0005_upload_content_hash

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_upload_content_hash"
down_revision: str | None = "0004_blob_attachment_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("DELETE FROM upload_capability"))


def downgrade() -> None:
    pass
