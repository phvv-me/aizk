# Give a conversion Docling's own policy check will never pass a state retry stops offering.
# Revision ID 0009_unreadable_conversions

from alembic import op

revision: str = "0009_unreadable_conversions"
down_revision: str | None = "0008_storage_footprint"
branch_labels: str | None = None
depends_on: str | None = None

STATE = "artifact_content_state"
VALUE = "unreadable"


def upgrade() -> None:
    """Add the `unreadable` value beside `failed` on the durable conversion state.

    `failed` already covers a timeout, a network blip, or a Docling restart, every one of
    them worth `aizk admin queue retry conversion` offering back to the queue. `unreadable`
    names the other outcome, where Docling's own policy check refused the bytes because the
    format itself is unreadable, a verdict the exact same request would reach again on every
    retry. Splitting it out is what lets the retry query and the operator doctor stop
    treating a bounded, expected fact about the corpus as an open incident, while the row and
    its stored `error` stay exactly where they were for anyone who asks what was rejected and
    why.
    """
    op.execute(f"ALTER TYPE {STATE} ADD VALUE IF NOT EXISTS '{VALUE}'")


def downgrade() -> None:
    """Fold `unreadable` rows back to `failed` and leave the enum value in the catalog.

    PostgreSQL cannot drop a value from an enum type, so the type itself never returns to
    its pre-migration shape, and no downgrade of this revision can honestly promise that. What
    it can promise is that the schema stays safe for the code it is downgrading to, an older
    `ArtifactContent.State` that has never heard of `unreadable` and would fail to map a row
    carrying it. Folding those rows back to `failed` is the same bucket this migration split
    them out of, so the downgrade is a real, if approximate, inverse of the data it touched.
    """
    op.execute(f"UPDATE artifact_content SET state = 'failed' WHERE state = '{VALUE}'")
