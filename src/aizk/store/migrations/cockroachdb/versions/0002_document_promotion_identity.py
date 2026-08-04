# Give a promoted document copy one database-owned identity per destination on CockroachDB.
# Revision ID 0002_document_promotion_identity

from importlib import import_module
from typing import Protocol, cast

from alembic import op

revision: str = "0002_document_promotion_identity"
down_revision: str | None = "0001_cockroachdb"
branch_labels: str | None = None
depends_on: str | None = None


class PromotionIdentity(Protocol):
    """The portable repair steps shared with the PostgreSQL revision of the same name."""

    INDEX: str

    def sort_scope_arrays(self) -> None: ...

    def assert_one_copy_per_destination(self) -> None: ...


# CockroachDB runs its own migration branch, so the rule the PostgreSQL revision installs
# would never reach an existing CockroachDB deployment without this twin. The repair steps
# themselves are portable and belong to one revision, so they are imported rather than
# restated, exactly as this branch's base revision imports the shared ontology seeds.
_shared = cast(
    "PromotionIdentity",
    import_module("aizk.store.migrations.versions.0006_document_promotion_identity"),
)


def upgrade() -> None:
    """Make one source stand for at most one copy per destination scope set.

    A database built by this branch's base revision already carries the index, because that
    revision creates the schema from the mapped metadata the rule now lives in. Only a
    deployment created before it needs the index built here, so the statement tolerates one
    that already stands rather than assuming which of the two it is running against.
    """
    _shared.sort_scope_arrays()
    _shared.assert_one_copy_per_destination()
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_shared.INDEX} ON document "
        "(promoted_from, scopes) WHERE promoted_from IS NOT NULL"
    )


def downgrade() -> None:
    """Allow several copies of one source per destination again."""
    op.execute(f"DROP INDEX IF EXISTS {_shared.INDEX}")
