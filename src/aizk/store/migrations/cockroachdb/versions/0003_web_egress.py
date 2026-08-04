# Account for public web calls and mark the pages they cache, on CockroachDB.
# Revision ID 0003_web_egress

from importlib import import_module
from typing import Protocol, cast

from alembic import op

revision: str = "0003_web_egress"
down_revision: str | None = "0002_document_promotion_identity"
branch_labels: str | None = None
depends_on: str | None = None


class WebEgress(Protocol):
    """The names the PostgreSQL revision of the same change already settled."""

    OPERATION: str
    ORIGIN: str
    ADDED: tuple[str, ...]


# CockroachDB runs its own migration branch, so the PostgreSQL revision would never reach a
# CockroachDB deployment. The names belong to one revision, so they are imported rather than
# restated, exactly as this branch's earlier revisions import from their twins.
_shared = cast(
    "WebEgress",
    import_module("aizk.store.migrations.versions.0007_web_egress"),
)


def upgrade() -> None:
    """Add the metered web operations and the document origin the cache marks.

    A database built by this branch's base revision already carries the origin column, since
    that revision creates the schema from the mapped metadata this rule now lives in, so the
    statements tolerate what already stands rather than assuming which case they are running
    against. CockroachDB has no transactional `CREATE TYPE` alongside `ALTER TYPE`, so the
    enum is created and extended in separate statements.
    """
    op.execute(f"CREATE TYPE IF NOT EXISTS {_shared.ORIGIN} AS ENUM ('authored', 'web_cache')")
    for value in _shared.ADDED:
        op.execute(f"ALTER TYPE {_shared.OPERATION} ADD VALUE IF NOT EXISTS '{value}'")
    op.execute(
        f"ALTER TABLE document ADD COLUMN IF NOT EXISTS origin {_shared.ORIGIN} "
        "NOT NULL DEFAULT 'authored'"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_document_origin ON document (origin)")


def downgrade() -> None:
    """Drop the origin marker, leaving the enum values in place."""
    op.execute("DROP INDEX IF EXISTS ix_document_origin")
    op.execute("ALTER TABLE document DROP COLUMN IF EXISTS origin")
    op.execute(f"DROP TYPE IF EXISTS {_shared.ORIGIN}")
