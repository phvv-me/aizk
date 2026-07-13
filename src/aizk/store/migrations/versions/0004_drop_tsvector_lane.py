# The tsvector lexical fallback is retired: vchord_bm25 is the only lexical lane, so the
# generated tsv column and its GIN index leave the chunk table. Fresh databases never
# create them anymore, hence the IF EXISTS guards.

from collections.abc import Sequence

from alembic import op

revision: str = "0004_drop_tsvector_lane"
down_revision: str | None = "0003_fuzzy_mentions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunk_tsv")
    op.execute("ALTER TABLE chunk DROP COLUMN IF EXISTS tsv")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE chunk ADD COLUMN tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', coalesce(lexical, text))) STORED"
    )
    op.execute("CREATE INDEX ix_chunk_tsv ON chunk USING gin (tsv)")
