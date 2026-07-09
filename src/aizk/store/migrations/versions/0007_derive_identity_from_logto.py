"""Drop the user, group, and membership tables; derive identity and org scopes from Logto.

aizk keeps no local identity any more. A scoped row's `owner_id` becomes `uuid5(oidc_subject)` and
its `scopes` become `uuid5(oidc_org_id)` values, both derived from the verified token with no
lookup row, and the row level security lattice reads the caller's standing from per-transaction
GUCs (`app.orgs`, `app.writable_orgs`) rather than a membership subquery. This migration remaps the
existing surrogate ids to their derived values while the mapping tables are still present, drops
the scoped policies that referenced `membership`, drops the three identity tables and the
group-delete trigger, then re-applies the model's GUC-based policies.

The remap is a clean no-op on an empty database (a fresh test schema or a new deployment), and
best-effort on a populated one: a row owned by a user that was never linked to an OIDC subject
keeps its old surrogate owner id, unreachable by any token, since there is no subject to derive a
stable id from. Groups all carry `oidc_org_id` (required since `0006`), so every shared scope
remaps cleanly. There is no faithful reverse.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from aizk.store.identity import org_uuid, user_uuid
from alembic import op

revision: str = "0007_derive_identity_from_logto"
down_revision: str | None = "0006_group_oidc_org_required"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# every table the `Scoped` mixin gives an `owner_id` and a `scopes` column, so every table whose
# ids the remap rewrites and whose policies the re-apply regenerates.
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


def remap_scopes(connection: sa.Connection) -> None:
    """Rewrite every scoped row's `scopes`, replacing each old group id with `uuid5(oidc_org_id)`.

    Runs while `group_` is still present, one `array_replace` per group per table, so a shared row
    naming a group by its old surrogate id comes out naming the same org by its derived scope.
    """
    groups = connection.execute(sa.text("SELECT id, oidc_org_id FROM group_")).all()
    for old_id, oidc_org_id in groups:
        new_id = org_uuid(oidc_org_id)
        for table in SCOPED_TABLES:
            connection.execute(
                sa.text(
                    f"UPDATE {table} SET scopes = "
                    "array_replace(scopes, CAST(:old AS uuid), CAST(:new AS uuid))"
                ),
                {"old": str(old_id), "new": str(new_id)},
            )


def remap_owners(connection: sa.Connection) -> None:
    """Rewrite every scoped row's `owner_id` from its surrogate user id to `uuid5(oidc_subject)`.

    Only linked users remap, since a user with no OIDC subject has no stable id to derive; its rows
    keep their surrogate owner and stay reachable only under the owner role.
    """
    users = connection.execute(
        sa.text("SELECT id, oidc_subject FROM user_ WHERE oidc_subject IS NOT NULL")
    ).all()
    for old_id, oidc_subject in users:
        new_id = user_uuid(oidc_subject)
        for table in SCOPED_TABLES:
            connection.execute(
                sa.text(
                    f"UPDATE {table} SET owner_id = CAST(:new AS uuid) "
                    "WHERE owner_id = CAST(:old AS uuid)"
                ),
                {"old": str(old_id), "new": str(new_id)},
            )


def upgrade() -> None:
    connection = op.get_bind()
    remap_scopes(connection)
    remap_owners(connection)
    # drop the old scoped policies first: they carry a `membership` subquery, so a plain
    # `DROP TABLE membership` would refuse while a policy still depends on it. The names are stable
    # (`scope_read`/`scope_insert`/`scope_update`/`scope_delete`), so this drops whichever bodies
    # `0001`/`0003` installed, and the re-apply below recreates them from the GUC-based model.
    for table in SCOPED_TABLES:
        op.drop_scoped_rls(table)
    # the group-delete demotion has nowhere left to fire: `group_` and its trigger go together, so
    # the standalone function is dropped explicitly.
    op.execute("DROP FUNCTION IF EXISTS demote_group_scopes() CASCADE")
    op.drop_table("membership")
    # CASCADE clears the dependent foreign keys: `group_` is no longer referenced once membership
    # is gone, and dropping `user_` clears the `owner_id` foreign key `0001` put on every scoped
    # table, leaving the column itself in place carrying the now-derived owner ids.
    op.execute("DROP TABLE group_ CASCADE")
    op.execute("DROP TABLE user_ CASCADE")
    for table in SCOPED_TABLES:
        op.apply_scoped_rls(table)


def downgrade() -> None:
    raise NotImplementedError("deriving identity from Logto has no faithful reverse")
