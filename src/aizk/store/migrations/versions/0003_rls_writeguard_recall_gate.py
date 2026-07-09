"""Close the RLS write-guard empty-scope bypass and gate curated pending facts out of recall.

Two security fixes that live in generated DDL, so they land as a migration rather than a model
change alone. First, re-apply the scoped row level security on every tenant-scoped table so its
write-check policies pick up `ScopeLattice.write`'s corrected empty-scope branch: the old
`scopes <@ writer_groups` admitted any private (empty-scope) row regardless of owner, since
`'{}' <@ anything` is trivially true in Postgres, letting an authenticated caller forge rows into
another user's private space. Second, recreate `hybrid_recall` so its two fact CTEs carry the
reviewed_at gate the ORM read path already applies, keeping a curated group's unreviewed pending
claims out of another member's default recall.
"""

import importlib.resources
from collections.abc import Sequence

from jinja2 import Environment

from aizk.config import settings
from alembic import op

revision: str = "0003_rls_writeguard_recall_gate"
down_revision: str | None = "0002_group_oidc_org"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# the tenant-scoped tables whose write-check policies compile from `ScopeLattice.write`, the ones
# the empty-scope owner-guard fix regenerates; the content tables carry a different, untouched set.
SCOPED_TABLES = (
    "document",
    "chunk",
    "entity_claim",
    "fact_claim",
    "community",
    "profile",
    "session_item",
    "watermark",
)
# hybrid_recall's overload identity for the DROP, bare parameter types only, the same spelling
# `0001_init` drops it by.
HYBRID_RECALL_TYPES = "halfvec, text, int, int, int"
SQL_DIR = importlib.resources.files("aizk.store.migrations") / "sql"


def render_sql(name: str, **context: str | float) -> str:
    """Render a `.sql.j2` migration template, the helper `0001_init` creates the function with.

    name: filename under `store/migrations/sql/`.
    context: the template's own branching variables.
    """
    return Environment().from_string((SQL_DIR / name).read_text()).render(**context)


def rebuild() -> None:
    """Regenerate the scoped write policies and the recall function from the current source.

    Both upgrade and downgrade run this: the policy shape and the function body are generated from
    the models and the template, so re-applying is the only reverse there is, and reintroducing a
    fixed security hole on downgrade is not a behavior worth preserving.
    """
    for table in SCOPED_TABLES:
        op.drop_scoped_rls(table)
        op.apply_scoped_rls(table)
    op.execute(f"DROP FUNCTION IF EXISTS hybrid_recall({HYBRID_RECALL_TYPES})")
    op.execute(
        render_sql(
            "hybrid_recall.sql.j2",
            bm25_backend=settings.bm25_backend,
            promoted_bonus=settings.promoted_bonus,
            bm25_index="ix_chunk_bm25",
            bm25_tokenizer="aizk_bm25",
        )
    )


def upgrade() -> None:
    rebuild()


def downgrade() -> None:
    rebuild()
