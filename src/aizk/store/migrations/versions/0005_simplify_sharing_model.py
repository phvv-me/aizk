"""Drop curation and server-admin from the schema, align role names, add the group-delete trigger.

The model no longer declares a curation-review loop, a server-wide admin flag, or a content-delete
policy, and this brings the schema to match. It drops the `reviewed_at`, `curated`, and `is_admin`
columns and the admin-gated policies that referenced the flag, renames the membership roles to
Logto's own `viewer`/`editor`/`admin`, recreates `live_fact` and `hybrid_recall` without the
reviewed_at gate, and replaces the app-side `Group.demote_scoped_rows` with a `BEFORE DELETE`
trigger so a deleted group's scoped rows fall back to private on every delete path rather than only
the one code path that remembered to call the method.
"""

import importlib.resources
from collections.abc import Sequence

from jinja2 import Environment

from aizk.config import settings
from aizk.store.mixins.view import create_view_ddl, drop_view_ddl
from aizk.store.models.views.live_fact import LiveFact
from alembic import op

revision: str = "0005_simplify_sharing_model"
down_revision: str | None = "0004_ontology_snake_case"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
HYBRID_RECALL_TYPES = "halfvec, text, int, int, int"
SQL_DIR = importlib.resources.files("aizk.store.migrations") / "sql"

# the DB-native replacement for the app-side demotion: on deleting a group, first drop a claim
# about to be demoted when its owner already privately holds the same content (else the two would
# collide on the empty scope set), then reset every scoped row naming the group to fully private.
# SECURITY DEFINER so it reaches every owner's rows past row level security, the same owner-role
# reach the old app method opened its own admin engine for. It widens privacy, never narrows it: an
# intersection `{A, B}` resets to `{}` rather than to `{A}`, since dropping one group from a set
# would broaden, not narrow, who can read the row.
DEMOTE_FUNCTION = """
CREATE OR REPLACE FUNCTION demote_group_scopes() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    DELETE FROM entity_claim d USING entity_claim p
     WHERE d.scopes @> ARRAY[OLD.id]::uuid[]
       AND cardinality(p.scopes) = 0
       AND p.owner_id = d.owner_id AND p.content_id = d.content_id;
    DELETE FROM fact_claim d USING fact_claim p
     WHERE d.scopes @> ARRAY[OLD.id]::uuid[] AND upper_inf(d.recorded)
       AND cardinality(p.scopes) = 0 AND upper_inf(p.recorded)
       AND p.owner_id = d.owner_id AND p.content_id = d.content_id;
    UPDATE document     SET scopes = '{}' WHERE scopes @> ARRAY[OLD.id]::uuid[];
    UPDATE chunk        SET scopes = '{}' WHERE scopes @> ARRAY[OLD.id]::uuid[];
    UPDATE entity_claim SET scopes = '{}' WHERE scopes @> ARRAY[OLD.id]::uuid[];
    UPDATE fact_claim   SET scopes = '{}' WHERE scopes @> ARRAY[OLD.id]::uuid[];
    UPDATE community    SET scopes = '{}' WHERE scopes @> ARRAY[OLD.id]::uuid[];
    UPDATE profile      SET scopes = '{}' WHERE scopes @> ARRAY[OLD.id]::uuid[];
    UPDATE session_item SET scopes = '{}' WHERE scopes @> ARRAY[OLD.id]::uuid[];
    UPDATE watermark    SET scopes = '{}' WHERE scopes @> ARRAY[OLD.id]::uuid[];
    RETURN OLD;
END;
$$
"""
DEMOTE_TRIGGER = (
    "CREATE TRIGGER group_demote_scopes BEFORE DELETE ON group_ "
    "FOR EACH ROW EXECUTE FUNCTION demote_group_scopes()"
)


def render_hybrid_recall() -> str:
    """Render the recall function from its template, the same way `0001_init` first created it."""
    template = (SQL_DIR / "hybrid_recall.sql.j2").read_text()
    return (
        Environment()
        .from_string(template)
        .render(
            bm25_backend=settings.bm25_backend,
            promoted_bonus=settings.promoted_bonus,
            bm25_index="ix_chunk_bm25",
            bm25_tokenizer="aizk_bm25",
        )
    )


def upgrade() -> None:
    # rename the user table to the `user_` its model now derives (USER being reserved, the same
    # trailing-underscore `group_` carries); dependent foreign keys follow the rename automatically
    op.rename_table("users", "user_")
    # the recall function and live_fact view both read reviewed_at, so they go before the column
    op.execute(f"DROP FUNCTION IF EXISTS hybrid_recall({HYBRID_RECALL_TYPES})")
    op.execute(drop_view_ddl(LiveFact.__tablename__))
    # the admin-gated policies the models no longer declare; drop_scoped_rls only knows the current
    # model's policies, so these lingering ones need an explicit drop by name
    for policy in ("curation_admin_read", "curation_admin_update", "curation_admin_delete"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON fact_claim")
    for table in ("entity_content", "fact_content"):
        op.execute(f"DROP POLICY IF EXISTS content_delete ON {table}")
    op.drop_column("fact_claim", "reviewed_at")
    op.drop_column("group_", "curated")
    op.drop_column("user_", "is_admin")
    # rename the membership roles into Logto's own vocabulary, so a token's org role needs no map.
    # guarded so it is a no-op on a database `0001` already seeded with the new names, renaming
    # only an older one still carrying reader/writer (crimson upgrading through this migration)
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel = 'reader' "
        "AND enumtypid = 'membership_role'::regtype) THEN "
        "ALTER TYPE membership_role RENAME VALUE 'reader' TO 'viewer'; END IF; "
        "IF EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel = 'writer' "
        "AND enumtypid = 'membership_role'::regtype) THEN "
        "ALTER TYPE membership_role RENAME VALUE 'writer' TO 'editor'; END IF; "
        "END $$"
    )
    # regenerate the scoped write policies from the model so they gate on editor/admin, then
    # rebuild the view and recall function from the reviewed_at-free source
    for table in SCOPED_TABLES:
        op.drop_scoped_rls(table)
        op.apply_scoped_rls(table)
    op.execute(create_view_ddl(LiveFact.__tablename__, LiveFact.__view_select__()))
    op.execute(render_hybrid_recall())
    op.execute(DEMOTE_FUNCTION)
    op.execute(DEMOTE_TRIGGER)


def downgrade() -> None:
    raise NotImplementedError("the curation and server-admin removal has no faithful reverse")
