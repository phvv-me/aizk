# Shrink what the store keeps on disk and record how each piece was laid out, on CockroachDB.
# Revision ID 0004_storage_footprint

from importlib import import_module
from typing import Protocol, cast

from alembic import op

revision: str = "0004_storage_footprint"
down_revision: str | None = "0003_web_egress"
branch_labels: str | None = None
depends_on: str | None = None


class StorageFootprint(Protocol):
    """The derivative columns the PostgreSQL revision of the same change already settled.

    Only the keys are read here, since blanking a value before dropping it is a PostgreSQL
    concern this backend has no use for.
    """

    DERIVATIVES: dict[str, str]


# CockroachDB runs its own migration branch, so the PostgreSQL revision would never reach a
# CockroachDB deployment. The names belong to one revision, so they are imported rather than
# restated, exactly as this branch's earlier revisions import from their twins.
_shared = cast(
    "StorageFootprint",
    import_module("aizk.store.migrations.versions.0008_storage_footprint"),
)


def upgrade() -> None:
    """Mark each object's compression policy and drop the unread artifact derivatives.

    The half precision embedding columns of the PostgreSQL twin have no counterpart here.
    CockroachDB has no `halfvec` type, so this branch keeps the portable full `VECTOR` column
    and there is nothing to convert, which is why this revision is shorter than its twin
    rather than incomplete.

    A database this branch's base revision built already carries the level marker and both
    derivative columns, since that revision creates the schema from the mapped metadata the
    marker now lives in, so every statement tolerates what already stands rather than
    assuming which case it is running against. A deployment created before the marker joined
    the metadata is the one that needs it built here, and it needs it badly, since every blob
    write states `encoding_level`.

    CockroachDB stores a row's values inline in its key-value ranges and has no TOAST
    relation, so the blanking `UPDATE` the PostgreSQL twin needs to reclaim out-of-line bytes
    has no counterpart. The drop is asynchronous and the storage engine compacts the ranges
    on its own schedule, with no `VACUUM FULL` to run.
    """
    # An object starts null, which reads as laid out under an unknown policy, so the first
    # `aizk admin storage compact` pass re-evaluates it and stamps what it decided.
    op.execute("ALTER TABLE blob ADD COLUMN IF NOT EXISTS encoding_level INT8")
    op.execute(
        "ALTER TABLE blob ADD CONSTRAINT IF NOT EXISTS ck_blob_encoding_level_range "
        "CHECK (encoding_level IS NULL OR encoding_level BETWEEN 1 AND 22)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_blob_encoding_level ON blob (encoding_level)")
    for name in _shared.DERIVATIVES:
        op.execute(f"ALTER TABLE artifact_content DROP COLUMN IF EXISTS {name}")
    op.execute("ALTER TABLE artifact_content ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMPTZ")


def downgrade() -> None:
    """Restore the derivative columns empty, since their content is reproducible, not saved."""
    op.execute("ALTER TABLE artifact_content DROP COLUMN IF EXISTS indexed_at")
    op.execute("ALTER TABLE artifact_content ADD COLUMN IF NOT EXISTS docling_json JSONB")
    op.execute(
        "ALTER TABLE artifact_content ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'"
    )
    op.execute("DROP INDEX IF EXISTS ix_blob_encoding_level")
    op.execute("ALTER TABLE blob DROP CONSTRAINT IF EXISTS ck_blob_encoding_level_range")
    op.execute("ALTER TABLE blob DROP COLUMN IF EXISTS encoding_level")
