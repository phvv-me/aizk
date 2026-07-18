# Close the blob authorization laundering path and bind content to its artifact.
# Revision ID 0004_blob_attachment_guard

from collections.abc import Sequence

from alembic import op

revision: str = "0004_blob_attachment_guard"
down_revision: str | None = "0003_upload_capability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# A caller may attach a blob to a new content row only when the blob is brand new
# (referenced by nothing yet, the upload path) or already reachable through a content
# revision the caller can read (the share path). The function runs as the migration
# owner, which bypasses row security, so it sees the true global set of references
# rather than only the caller's own rows.
_ATTACHABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION artifact_content_blob_attachable(target_blob uuid)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  standing jsonb := coalesce(nullif(current_setting('app.scopes', true), ''), '{}')::jsonb;
  readable uuid[] := ARRAY(
    SELECT value::uuid FROM jsonb_array_elements_text(standing -> 'read')
  );
  shareable uuid[] := ARRAY(
    SELECT value::uuid FROM jsonb_array_elements_text(standing -> 'public')
  );
BEGIN
  IF NOT EXISTS (SELECT 1 FROM artifact_content WHERE blob_id = target_blob) THEN
    RETURN true;
  END IF;
  RETURN EXISTS (
    SELECT 1
    FROM artifact_content c
    WHERE c.blob_id = target_blob
      AND (
        c.scopes <@ readable
        OR (cardinality(c.scopes) = 1 AND c.scopes <@ shareable)
      )
  );
END;
$$;
"""

# One guard on the mutable content table: reject a blob the caller cannot legitimately
# attach, and keep `blob_id` immutable once set so a committed row can never be
# re-pointed at foreign bytes.
_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION artifact_content_guard_blob()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.blob_id IS DISTINCT FROM OLD.blob_id THEN
    RAISE EXCEPTION 'artifact_content.blob_id is immutable'
      USING ERRCODE = 'restrict_violation';
  END IF;
  IF TG_OP = 'INSERT' AND NOT artifact_content_blob_attachable(NEW.blob_id) THEN
    RAISE EXCEPTION 'blob % is not attachable by this caller', NEW.blob_id
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_artifact_content_blob",
        "artifact_content",
        ["artifact_id", "blob_id"],
    )
    op.create_unique_constraint(
        "uq_artifact_content_artifact_id_id",
        "artifact_content",
        ["artifact_id", "id"],
    )

    op.drop_constraint(
        "fk_document_artifact_content_id_artifact_content",
        "document",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_document_artifact_content_pair",
        "document",
        "artifact_content",
        ["artifact_id", "artifact_content_id"],
        ["artifact_id", "id"],
        ondelete="SET NULL",
    )

    op.execute(_ATTACHABLE_FUNCTION)
    op.execute(_GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER artifact_content_guard_blob "
        "BEFORE INSERT OR UPDATE ON artifact_content "
        "FOR EACH ROW EXECUTE FUNCTION artifact_content_guard_blob()"
    )


def downgrade() -> None:
    raise NotImplementedError("artifact storage has no lossless reverse migration")
