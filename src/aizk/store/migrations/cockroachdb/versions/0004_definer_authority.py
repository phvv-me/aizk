# Restore the session carrier visible inside CockroachDB security definers.
# Revision ID 0004_definer_authority

from aizk.config import settings
from aizk.store.migrations.cockroachdb import scoped_cspann
from alembic import op

revision: str = "0004_definer_authority"
down_revision: str | None = "0003_object_retirement"
branch_labels: str | None = None
depends_on: str | None = None


def replace(*, definer_safe: bool) -> None:
    """Replace both read capabilities and reassert their privilege switch."""
    op.execute(scoped_cspann.scope_function(definer_safe=definer_safe, replace=True))
    op.execute(
        scoped_cspann.search_function(
            settings.embed_dim,
            definer_safe=definer_safe,
            replace=True,
        )
    )
    op.execute("ALTER FUNCTION aizk_private.cspann_scopes(STRING) SECURITY DEFINER")
    op.execute(
        "ALTER FUNCTION aizk_private.cspann_search("
        f"STRING, UUID[], VECTOR({settings.embed_dim}), INT8) SECURITY DEFINER"
    )


def upgrade() -> None:
    """Use the supported session variable that survives a definer privilege switch."""
    replace(definer_safe=True)


def downgrade() -> None:
    """Restore the typed custom settings used before the compatibility repair."""
    replace(definer_safe=False)
