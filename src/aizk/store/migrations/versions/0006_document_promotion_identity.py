# Give a promoted document copy one database-owned identity per destination.
# Revision ID 0006_document_promotion_identity

import sqlalchemy as sa

from alembic import context, op

revision: str = "0006_document_promotion_identity"
down_revision: str | None = "0005_monthly_quota"
branch_labels: str | None = None
depends_on: str | None = None

INDEX = "uq_document_promotion_scope"
SORTED = "ARRAY(SELECT unnest(scopes) ORDER BY 1)"


def sort_scope_arrays() -> None:
    """Put every document's scope array in ascending order.

    A scope set is stored as an ordered array, so both equality and the unique index below
    mean set identity only while the arrays are sorted. Every ORM writer sorts, but a row
    written before that invariant was relied upon, or inserted around the ORM, need not be,
    and one unsorted row would slip past the index carrying a set a sorted row already has.
    """
    op.execute(f"UPDATE document SET scopes = {SORTED} WHERE scopes IS DISTINCT FROM {SORTED}")


def assert_one_copy_per_destination() -> None:
    """Fail naming the offending pairs, rather than leaving an index build to fail unhelpfully.

    This runs only once the arrays are sorted, so two rows carrying one scope set in two
    orders are recognised as the duplicate they are instead of passing as distinct. A script
    generated offline has no connection to read, so there the index build is the only guard
    and a deployment holding duplicates learns about them from the build itself.
    """
    if context.is_offline_mode():
        return
    duplicated = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT promoted_from, scopes, count(*) AS copies FROM document "
                "WHERE promoted_from IS NOT NULL GROUP BY promoted_from, scopes "
                "HAVING count(*) > 1 ORDER BY promoted_from"
            )
        )
        .all()
    )
    if not duplicated:
        return
    offenders = "; ".join(
        f"source {source} has {copies} copies in {scopes}" for source, scopes, copies in duplicated
    )
    raise RuntimeError(
        "cannot give promoted copies one identity per destination while duplicates stand. "
        "Retire or merge the extra copies, then migrate again: " + offenders
    )


def upgrade() -> None:
    """Make one source stand for at most one copy per destination scope set.

    Sharing looked a standing copy up and inserted one when it found none, which two
    concurrent shares could both do. The partial unique index makes the second insert fail
    instead, so a share resolves the winner rather than duplicating the destination. It also
    indexes the `promoted_from` lookup every share performs.

    The index builds blocking rather than concurrently, deliberately. This schema serves
    single-instance deployments whose document table is far too small for the build to be
    felt, and a blocking build keeps the migration one transaction that either lands whole
    or not at all, which a concurrent build cannot offer.
    """
    sort_scope_arrays()
    assert_one_copy_per_destination()
    op.create_index(
        INDEX,
        "document",
        ["promoted_from", "scopes"],
        unique=True,
        postgresql_where=sa.text("promoted_from IS NOT NULL"),
    )


def downgrade() -> None:
    """Allow several copies of one source per destination again."""
    op.drop_index(INDEX, table_name="document")
