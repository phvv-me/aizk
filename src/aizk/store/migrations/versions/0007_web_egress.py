# Account for public web calls and mark the pages they cache.
# Revision ID 0007_web_egress

import sqlalchemy as sa

from alembic import op

revision: str = "0007_web_egress"
down_revision: str | None = "0006_document_promotion_identity"
branch_labels: str | None = None
depends_on: str | None = None

OPERATION = "usage_event_operation"
ORIGIN = "document_origin"
ADDED = ("web_search", "web_fetch")


def upgrade() -> None:
    """Add the two metered web operations and the document origin the cache marks.

    The `recall` operation keeps its stored name even though the tool is now called `find`.
    That value is the key every published usage report column, the browser dashboard, and
    the generated TypeScript client already read, so renaming it would break all of them to
    buy nothing an accountant would notice. The tool rename lives on the surfaces callers
    touch and stops at the ledger.

    Existing documents become `authored`, which is what they are. Only a page `find` fetches
    from a third party ever carries `web_cache`, and that value is what keeps such a page out
    of the graph, out of the ontology and insight passes, and under the web label wherever it
    surfaces.
    """
    for value in ADDED:
        op.execute(f"ALTER TYPE {OPERATION} ADD VALUE IF NOT EXISTS '{value}'")
    origin = sa.Enum("authored", "web_cache", name=ORIGIN)
    origin.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "document",
        sa.Column("origin", origin, nullable=False, server_default="authored"),
    )
    op.create_index("ix_document_origin", "document", ["origin"])


def downgrade() -> None:
    """Drop the origin marker, leaving the enum values in place.

    PostgreSQL cannot remove a value from an enum type in use, and the two web operations
    are harmless to a schema that no longer writes them, so the reversible half of this
    revision is the column and its index alone.
    """
    op.drop_index("ix_document_origin", table_name="document")
    op.drop_column("document", "origin")
    sa.Enum(name=ORIGIN).drop(op.get_bind(), checkfirst=True)
