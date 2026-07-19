# Squashed schema with Logto identity, the scope lattice, immutable content, and forced RLS.
# This single revision folds the entire historical chain (former 0001..0006): the knowledge
# graph, artifacts and usage accounting, the durable upload capability store, the blob
# attachment guard, and the least-authority upload ticket. The purge migrations 0005 and 0006
# only deleted rows from an already-empty table, so they leave no schema trace here.
#
# Fresh installs and CI build the whole schema from this file. The crimson production database
# is already migrated to the old 0006 head with an identical schema, so it is reconciled not by
# re-running anything but by restamping it onto this revision:
#     alembic stamp 0001_init   (equivalently: UPDATE alembic_version SET version_num='0001_init')
# Revision ID 0001_init

from collections.abc import Sequence

import rls
import sqlalchemy as sa
from inflection import underscore
from pgvector.sqlalchemy import HALFVEC
from rls.alembic import AlterRLSOp
from sqlalchemy import Select
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB, TSTZRANGE
from sqlmodel import select

from aizk.config import settings
from aizk.store.ddl import CreateView
from alembic import op

# Migration-local ontology seeds preserve this revision independently of current application code.
ENTITY_KINDS: tuple[tuple[str, str, str, bool], ...] = (
    # Structural kinds are system-written.
    ("RaptorSummary", "A recursive summary tree node built above entity clusters.", "core", True),
    ("Observation", "A reflective insight the system derives from existing facts.", "core", True),
    # General kinds
    (
        "Concept",
        "A catch-all for an idea or topic that fits no more specific type.",
        "general",
        False,
    ),
    # Management concepts are ordinary ontology kinds. Explicit declarations are only
    # the deterministic fast path and model extraction may infer them from prose.
    (
        "Project",
        "A concrete effort with a start and an end that produces a result, the unit a projects "
        "rollup treats as a node and its member notes as parts.",
        "general",
        False,
    ),
    (
        "Area",
        "An ongoing domain of responsibility or identity with no end date, the container that "
        "holds projects and the notes that outlive any one of them.",
        "general",
        False,
    ),
    (
        "Status",
        "A managed effort's explicit lifecycle state, such as active, paused, or completed.",
        "general",
        False,
    ),
    ("Tool", "A named library, framework, or piece of software.", "general", False),
    ("Person", "A specific individual.", "general", False),
    ("Decision", "A choice made and the reasoning behind it.", "general", False),
    ("Pattern", "A reusable approach or standing preference.", "general", False),
    ("Gotcha", "A trap or surprising behavior worth remembering.", "general", False),
    ("Goal", "An aim being worked toward.", "general", False),
    # Coding
    ("Module", "A source code module or file.", "coding", False),
    ("Function", "A named function or method.", "coding", False),
    # Research
    ("Paper", "A published or preprint research paper.", "research", False),
    ("Author", "A paper's author.", "research", False),
    ("Theorem", "A proven mathematical statement.", "research", False),
    (
        "Lemma",
        "A supporting mathematical statement proved on the way to a theorem.",
        "research",
        False,
    ),
    ("Definition", "A precise statement fixing what a term means.", "research", False),
    ("Proof", "The argument establishing a theorem or lemma.", "research", False),
    ("Claim", "An assertion put forward as true, not yet proven.", "research", False),
    ("Hypothesis", "A proposed explanation offered for testing.", "research", False),
    ("Method", "A named technique or algorithm.", "research", False),
    ("Model", "A trained or specified model.", "research", False),
    (
        "Dataset",
        "A named collection of data used for training or evaluation.",
        "research",
        False,
    ),
    ("Benchmark", "A named evaluation suite or task.", "research", False),
    ("Metric", "A named measure a result is scored by.", "research", False),
    ("Result", "A reported outcome or measurement.", "research", False),
    ("Hyperparameter", "A configuration value set before training.", "research", False),
    ("Experiment", "A specific run or trial.", "research", False),
    ("Equation", "A named or numbered mathematical equation.", "research", False),
    ("CodeArtifact", "Code produced by or for a paper or method.", "research", False),
    ("Conjecture", "An unproven mathematical statement believed likely true.", "research", False),
    (
        "Corollary",
        "A statement following directly from a theorem already proven.",
        "research",
        False,
    ),
    # Finance
    (
        "Instrument",
        "A tradable financial asset such as a stock, bond, or fund.",
        "finance",
        False,
    ),
    ("Account", "A named financial account holding positions or cash.", "finance", False),
    ("Position", "A held quantity of one instrument in an account.", "finance", False),
    ("Strategy", "A named approach to allocating or trading.", "finance", False),
    ("Expense", "A recorded outflow of money.", "finance", False),
    ("Income", "A recorded inflow of money.", "finance", False),
    ("Budget", "A planned allocation of money over a period.", "finance", False),
    # Personal
    ("Habit", "A recurring behavior being tracked or built.", "personal", False),
    ("Milestone", "A significant dated achievement or event.", "personal", False),
    (
        "Possession",
        "A durable physical asset, such as a house, car, or belonging.",
        "personal",
        False,
    ),
)

RELATION_KINDS: tuple[tuple[str, str, str, bool], ...] = (
    ("observes", "The predicate every system-derived observation carries.", "core", True),
    ("because", "Connects a decision or pattern to its reason.", "general", False),
    (
        "avoids",
        "Connects a pattern or decision to something it steers clear of.",
        "general",
        False,
    ),
    (
        "related_to",
        "A generic, otherwise-unclassified connection between two things.",
        "general",
        False,
    ),
    ("depends_on", "One thing requires another to exist or function.", "general", False),
    ("part_of", "One thing is a component of another.", "general", False),
    (
        "has_status",
        "A managed effort has one current lifecycle state.",
        "general",
        False,
    ),
    ("contradicts", "One statement conflicts with another.", "general", False),
    ("supersedes", "One statement replaces an earlier one.", "general", False),
    ("implements", "A piece of code realizes a pattern, method, or decision.", "coding", False),
    ("fixes", "A piece of code resolves a gotcha or bug.", "coding", False),
    ("proves", "A proof or paper establishes a theorem or lemma.", "research", False),
    ("refutes", "A result or paper disproves a claim or hypothesis.", "research", False),
    ("cites", "A paper references another paper.", "research", False),
    ("extends", "One piece of work builds on another.", "research", False),
    ("uses", "A method or experiment employs a tool, dataset, or model.", "research", False),
    (
        "evaluates_on",
        "A method or model is evaluated against a benchmark or dataset.",
        "research",
        False,
    ),
    ("improves_over", "A result outperforms an earlier one.", "research", False),
    (
        "derived_from",
        "One thing is mathematically or empirically derived from another.",
        "research",
        False,
    ),
    ("authored_by", "A paper's author relation.", "research", False),
    ("reproduces", "A result independently confirms an earlier one.", "research", False),
    (
        "allocates_to",
        "An account or strategy assigns money to an instrument or position.",
        "finance",
        False,
    ),
    ("tracks", "A budget monitors an expense or income category.", "finance", False),
    ("owns", "A person holds a possession or account.", "personal", False),
    ("motivated_by", "A goal or habit is driven by a reason.", "personal", False),
)

_RELATION_POLICIES = {
    "has_status": "state",
    "observes": "event",
    "part_of": "set",
    "supersedes": "event",
}

revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Schema settings are frozen when the migration runs.
EMBED_DIM = settings.embed_dim
INDEX_BACKEND = settings.index_backend
BASE_EXTENSIONS = ("vector", "pg_trgm", "pgcrypto")
BM25_TOKENIZER = "aizk_bm25"
BM25_INDEX = "ix_chunk_bm25"
BM25_SCHEMAS = ("tokenizer_catalog", "bm25_catalog")
PROMOTED_BONUS = settings.promoted_bonus


def bm25_lexical_statements() -> list[str]:
    """The DDL that builds the vchord_bm25 lexical lane on the chunk table."""
    return [
        f"SELECT tokenizer_catalog.create_tokenizer('{BM25_TOKENIZER}', $$\n"
        'model = "llmlingua2"\n$$)',
        "ALTER TABLE chunk ADD COLUMN bm25 bm25vector",
        # Both lexical backends index the contextual text when it exists.
        "CREATE FUNCTION chunk_bm25_sync() RETURNS trigger AS $$ BEGIN "
        "NEW.bm25 := tokenizer_catalog.tokenize("
        f"coalesce(NEW.lexical, NEW.text), '{BM25_TOKENIZER}'); "
        "RETURN NEW; END; $$ LANGUAGE plpgsql",
        "CREATE TRIGGER chunk_bm25_sync BEFORE INSERT OR UPDATE OF text, lexical ON chunk "
        "FOR EACH ROW EXECUTE FUNCTION chunk_bm25_sync()",
        f"CREATE INDEX {BM25_INDEX} ON chunk USING bm25 (bm25 bm25_ops)",
        f"GRANT USAGE ON SCHEMA {', '.join(BM25_SCHEMAS)} TO {APP_ROLE}",
        *(
            f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {APP_ROLE}"
            for schema in BM25_SCHEMAS
        ),
    ]


def required_extensions(index_backend: str) -> tuple[str, ...]:
    """The extensions this schema needs given the vector index backend."""
    extensions = [*BASE_EXTENSIONS, "vchord_bm25", "pg_tokenizer"]
    if index_backend == "vchordrq":
        extensions.append("vchord")
    return tuple(extensions)


def vector_index_ddl(name: str, table: str, backend: str) -> str:
    """The CREATE INDEX statement for one embedding column under the selected index backend."""
    return f"CREATE INDEX {name} ON {table} USING {backend} (embedding halfvec_cosine_ops)"


_SCOPED_TABLES = {
    "chunk": (True, True, "document"),
    "community": (False, True, None),
    "document": (True, False, None),
    "entity_claim": (False, False, None),
    "fact_claim": (True, False, None),
    "profile": (True, False, None),
    "session_item": (True, False, None),
    "watermark": (True, False, None),
    "artifact": (True, False, None),
    "artifact_content": (True, False, "artifact"),
    # Usage rows are an append-only ledger: readable and insertable, never mutated.
    "usage_event": (False, False, None),
}

# Immutable content is visible through claims.
_CONTENT_TABLES = ("entity_content", "fact_content")


def _scope_authority(standing: sa.ColumnElement, permission: str) -> sa.ColumnElement:
    """Turn one JSON scope permission into a native PostgreSQL UUID array."""
    values = (
        sa.func.jsonb_array_elements_text(standing.op("->")(permission))
        .table_valued("value")
        .render_derived()
    )
    return sa.func.array(select(sa.cast(values.c.value, sa.Uuid())).scalar_subquery())


def scoped_rls(
    table_name: str,
    mutable: bool,
    deletable: bool,
    read_through: str | None,
) -> rls.RLSState:
    """Compile the nonempty scope lattice policies frozen into this revision."""
    table = sa.table(
        table_name,
        sa.column("scopes", ARRAY(sa.Uuid())),
        *(sa.column(f"{read_through}_id", sa.Uuid()),) if read_through else (),
    )
    scopes = table.c.scopes
    standing = rls.current_setting("scopes", JSONB(), prefix="app")
    writable = _scope_authority(standing, "write")
    nonempty = sa.func.cardinality(scopes) > 0
    if read_through:
        parent = sa.table(
            read_through,
            sa.column("id", sa.Uuid()),
            sa.column("scopes", ARRAY(sa.Uuid())),
        )
        parent_id = table.c[f"{read_through}_id"]
        read = parent_id.in_(select(parent.c.id))
        parent_scope = sa.tuple_(parent_id, scopes).in_(select(parent.c.id, parent.c.scopes))
    else:
        readable = _scope_authority(standing, "read")
        public = _scope_authority(standing, "public")
        read = sa.and_(
            nonempty,
            sa.or_(
                scopes.op("<@")(readable),
                sa.and_(
                    sa.func.cardinality(scopes) == 1,
                    scopes.op("<@")(public),
                ),
            ),
        )
        parent_scope = sa.true()
    write = sa.and_(nonempty, scopes.op("<@")(writable), parent_scope)
    policies = [
        rls.Policy.select("scope_read", read, roles=(APP_ROLE,)),
        rls.Policy.insert("scope_insert", write, roles=(APP_ROLE,)),
    ]
    if mutable:
        policies.append(rls.Policy.update("scope_update", write, write, roles=(APP_ROLE,)))
    if deletable:
        policies.append(rls.Policy.delete("scope_delete", write, roles=(APP_ROLE,)))
    return rls.RLSState.declared(tuple(policies))


def content_rls(table_name: str) -> rls.RLSState:
    """Compile immutable content policies through the corresponding claim table."""
    claim_name = "entity_claim" if table_name == "entity_content" else "fact_claim"
    content = sa.table(table_name, sa.column("id", sa.Uuid()))
    claim = sa.table(claim_name, sa.column("content_id", sa.Uuid()))
    return rls.RLSState.declared(
        (
            rls.Policy.select(
                "content_read",
                content.c.id.in_(select(claim.c.content_id)),
                roles=(APP_ROLE,),
            ),
            rls.Policy.insert("content_insert", sa.true(), roles=(APP_ROLE,)),
        )
    )


def blob_rls() -> rls.RLSState:
    """Expose object metadata only through visible artifact revisions."""
    blob = sa.table("blob", sa.column("id", sa.Uuid()))
    content = sa.table("artifact_content", sa.column("blob_id", sa.Uuid()))
    return rls.RLSState.declared(
        (
            rls.Policy.select(
                "blob_read",
                blob.c.id.in_(select(content.c.blob_id)),
                roles=(APP_ROLE,),
            ),
            rls.Policy.insert("blob_insert", sa.true(), roles=(APP_ROLE,)),
        )
    )


def upload_capability_rls() -> rls.RLSState:
    """Compile the scope lattice for the single-use capability store: read, insert, delete."""
    table = sa.table("upload_capability", sa.column("scopes", ARRAY(sa.Uuid())))
    scopes = table.c.scopes
    standing = rls.current_setting("scopes", JSONB(), prefix="app")
    readable = _scope_authority(standing, "read")
    writable = _scope_authority(standing, "write")
    public = _scope_authority(standing, "public")
    nonempty = sa.func.cardinality(scopes) > 0
    read = sa.and_(
        nonempty,
        sa.or_(
            scopes.op("<@")(readable),
            sa.and_(sa.func.cardinality(scopes) == 1, scopes.op("<@")(public)),
        ),
    )
    write = sa.and_(nonempty, scopes.op("<@")(writable), sa.true())
    return rls.RLSState.declared(
        (
            rls.Policy.select("scope_read", read, roles=(APP_ROLE,)),
            rls.Policy.insert("scope_insert", write, roles=(APP_ROLE,)),
            rls.Policy.delete("scope_delete", write, roles=(APP_ROLE,)),
        )
    )


def live_fact_select() -> Select:
    """Build the view against the exact claim columns created by this revision."""
    claim = sa.table(
        "fact_claim",
        *(
            sa.column(name)
            for name in (
                "id",
                "content_id",
                "created_by",
                "scopes",
                "valid",
                "recorded",
                "last_accessed",
                "access_count",
                "attributes",
                "perspective_key",
                "source_chunk_id",
                "promoted_from",
            )
        ),
    )
    content = sa.table(
        "fact_content",
        *(
            sa.column(name)
            for name in ("id", "subject_id", "object_id", "predicate", "statement", "embedding")
        ),
    )
    columns = (
        claim.c.id,
        claim.c.content_id,
        content.c.subject_id,
        content.c.object_id,
        content.c.predicate,
        content.c.statement,
        content.c.embedding,
        claim.c.created_by,
        claim.c.scopes,
        claim.c.valid,
        claim.c.recorded,
        claim.c.last_accessed,
        claim.c.access_count,
        claim.c.attributes,
        claim.c.perspective_key,
        claim.c.source_chunk_id,
        claim.c.promoted_from,
    )
    current = sa.and_(
        sa.func.upper_inf(claim.c.recorded),
        sa.or_(claim.c.valid.is_(None), claim.c.valid.op("@>")(sa.func.now())),
    )
    return (
        select(columns[0], columns[1], columns[2], columns[3])
        .add_columns(*columns[4:])
        .select_from(claim.join(content, content.c.id == claim.c.content_id))
        .where(current)
    )


# Restricted role provisioned by initdb and granted access by this migration
APP_ROLE = "aizk_app"


# A caller may attach a blob to a new content row only when the blob is brand new (referenced by
# nothing yet, the upload path) or already reachable through a content revision the caller can
# read (the share path). The function runs as the migration owner, which bypasses row security,
# so it sees the true global set of references rather than only the caller's own rows.
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

# One guard on the mutable content table: reject a blob the caller cannot legitimately attach,
# and keep `blob_id` immutable once set so a committed row can never be re-pointed at foreign
# bytes.
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
    for extension in required_extensions(INDEX_BACKEND):
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")

    # Documents and chunks
    op.create_table(
        "document",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("subject_type", sa.Text(), nullable=True),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.Uuid(), nullable=False),
        sa.Column("promoted_from", sa.Uuid(), nullable=True),
        # A document may carry the source artifact revision it was ingested from.
        sa.Column("artifact_id", sa.Uuid(), nullable=True),
        sa.Column("artifact_content_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["promoted_from"], ["document.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_uri", "scopes", name="uq_document_source_scope"),
    )
    op.create_index("ix_document_content_hash", "document", ["content_hash"])
    op.create_index("ix_document_created_by", "document", ["created_by"])
    op.create_index("ix_document_expires_at", "document", ["expires_at"])
    op.create_index("ix_document_observed_at", "document", ["observed_at"])
    op.create_index("ix_document_scopes", "document", ["scopes"], postgresql_using="gin")
    op.create_index(
        "uq_document_subject_title_scope",
        "document",
        ["subject_type", "title", "scopes"],
        unique=True,
        postgresql_where=(
            sa.column("subject_type").is_not(None) & sa.column("title").is_not(None)
        ),
    )
    op.create_index("ix_document_artifact_id", "document", ["artifact_id"])
    op.create_index("ix_document_artifact_content_id", "document", ["artifact_content_id"])

    op.create_table(
        "chunk",
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("provenance", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("lexical", sa.Text(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        # A completed extraction is recorded even when it yields no claims.
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chunk_document_id", "chunk", ["document_id"])
    op.execute(vector_index_ddl("ix_chunk_embedding", "chunk", INDEX_BACKEND))
    op.create_index("ix_chunk_created_by", "chunk", ["created_by"])
    # Keep the pending work index proportional to outstanding work.
    op.create_index(
        "ix_chunk_pending", "chunk", ["id"], postgresql_where=sa.text("processed_at IS NULL")
    )
    op.create_index("ix_chunk_scopes", "chunk", ["scopes"], postgresql_using="gin")

    # Live ontology catalogs
    op.create_table(
        "entity_kind",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("structural", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_foreign_key(
        "fk_document_subject_type_entity_kind",
        "document",
        "entity_kind",
        ["subject_type"],
        ["name"],
    )
    op.create_table(
        "relation_kind",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("structural", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "policy",
            sa.Enum("set", "state", "event", name="relation_policy"),
            server_default="set",
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name"),
    )
    entity_kind_table = sa.table(
        "entity_kind",
        sa.column("name", sa.Text()),
        sa.column("description", sa.Text()),
        sa.column("domain", sa.Text()),
        sa.column("structural", sa.Boolean()),
    )
    op.bulk_insert(
        entity_kind_table,
        [
            {
                "name": underscore(name),
                "description": description,
                "domain": domain,
                "structural": structural,
            }
            for name, description, domain, structural in ENTITY_KINDS
        ],
    )
    relation_kind_table = sa.table(
        "relation_kind",
        sa.column("name", sa.Text()),
        sa.column("description", sa.Text()),
        sa.column("domain", sa.Text()),
        sa.column("structural", sa.Boolean()),
        sa.column(
            "policy",
            sa.Enum("set", "state", "event", name="relation_policy", create_type=False),
        ),
    )
    op.bulk_insert(
        relation_kind_table,
        [
            {
                "name": underscore(name),
                "description": description,
                "domain": domain,
                "structural": structural,
                "policy": _RELATION_POLICIES.get(underscore(name), "set"),
            }
            for name, description, domain, structural in RELATION_KINDS
        ],
    )

    # Immutable entity content and scoped claims
    op.create_table(
        "entity_content",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        sa.ForeignKeyConstraint(["type"], ["entity_kind.name"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(vector_index_ddl("ix_entity_content_embedding", "entity_content", INDEX_BACKEND))
    op.create_index(
        "ix_entity_content_name_lower",
        "entity_content",
        [sa.text("lower(name)")],
    )
    op.create_index(
        "ix_entity_content_name_trgm",
        "entity_content",
        [sa.text("lower(name) gin_trgm_ops")],
        postgresql_using="gin",
    )

    op.create_table(
        "entity_claim",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content_id", sa.Uuid(), nullable=False),
        sa.Column("attributes", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(["content_id"], ["entity_content.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_id", "scopes", name="uq_entity_claim_content_scope"),
    )
    op.create_index("ix_entity_claim_content_id", "entity_claim", ["content_id"])
    op.create_index("ix_entity_claim_created_by", "entity_claim", ["created_by"])
    op.create_index("ix_entity_claim_scopes", "entity_claim", ["scopes"], postgresql_using="gin")

    # Immutable fact content and bi-temporal scoped claims
    op.create_table(
        "fact_content",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("object_id", sa.Uuid(), nullable=True),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        sa.ForeignKeyConstraint(["predicate"], ["relation_kind.name"]),
        sa.ForeignKeyConstraint(["object_id"], ["entity_content.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_id"], ["entity_content.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(vector_index_ddl("ix_fact_content_embedding", "fact_content", INDEX_BACKEND))
    op.create_index("ix_fact_content_object_id", "fact_content", ["object_id"])
    op.create_index("ix_fact_content_subject_id", "fact_content", ["subject_id"])

    op.create_table(
        "fact_claim",
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content_id", sa.Uuid(), nullable=False),
        sa.Column("valid", TSTZRANGE(), nullable=True),
        sa.Column(
            "recorded",
            TSTZRANGE(),
            server_default=sa.text("tstzrange(now(), NULL, '[)')"),
            nullable=False,
        ),
        sa.Column("last_accessed", sa.DateTime(timezone=True), nullable=True),
        sa.Column("access_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attributes", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("perspective_key", sa.String(), server_default="world", nullable=False),
        sa.Column("source_chunk_id", sa.Uuid(), nullable=True),
        sa.Column("promoted_from", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["content_id"], ["fact_content.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["promoted_from"], ["fact_claim.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_chunk_id"], ["chunk.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fact_claim_content_id", "fact_claim", ["content_id"])
    op.create_index("ix_fact_claim_perspective_key", "fact_claim", ["perspective_key"])
    op.create_index("ix_fact_claim_created_by", "fact_claim", ["created_by"])
    op.create_index("ix_fact_claim_source_chunk_id", "fact_claim", ["source_chunk_id"])
    op.create_index("ix_fact_claim_promoted_from", "fact_claim", ["promoted_from"])
    op.create_index("ix_fact_claim_scopes", "fact_claim", ["scopes"], postgresql_using="gin")
    # Range and partial indexes serve as-of reads and enforce one live claim per scope set.
    op.create_index("ix_fact_claim_valid", "fact_claim", ["valid"], postgresql_using="gist")
    op.create_index("ix_fact_claim_recorded", "fact_claim", ["recorded"], postgresql_using="gist")
    op.create_index(
        "ix_fact_claim_live",
        "fact_claim",
        ["valid"],
        postgresql_using="gist",
        postgresql_where=sa.text("upper_inf(recorded)"),
    )
    op.create_index(
        "uq_fact_claim_live",
        "fact_claim",
        ["content_id", "scopes", "perspective_key"],
        unique=True,
        postgresql_where=sa.text("upper_inf(recorded)"),
    )

    # Community summaries
    op.create_table(
        "community",
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "member_ids", ARRAY(sa.Uuid()), server_default=sa.text("'{}'::uuid[]"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(vector_index_ddl("ix_community_embedding", "community", INDEX_BACKEND))
    op.create_index("ix_community_created_by", "community", ["created_by"])
    op.create_index("ix_community_scopes", "community", ["scopes"], postgresql_using="gin")

    # Entity profiles
    op.create_table(
        "profile",
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["subject_id"], ["entity_content.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scopes", "subject_id", name="uq_profile_scope_subject"),
    )
    op.execute(vector_index_ddl("ix_profile_embedding", "profile", INDEX_BACKEND))
    op.create_index("ix_profile_created_by", "profile", ["created_by"])
    op.create_index("ix_profile_scopes", "profile", ["scopes"], postgresql_using="gin")
    op.create_index("ix_profile_subject_id", "profile", ["subject_id"])

    # Session memory awaiting promotion
    op.create_table(
        "session_item",
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("provenance", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(vector_index_ddl("ix_session_item_embedding", "session_item", INDEX_BACKEND))
    op.create_index("ix_session_item_created_by", "session_item", ["created_by"])
    op.create_index("ix_session_item_promoted_at", "session_item", ["promoted_at"])
    op.create_index("ix_session_item_scopes", "session_item", ["scopes"], postgresql_using="gin")

    # Scope-local scheduler watermarks
    op.create_table(
        "watermark",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "entity_dirty",
                "fact_count",
                "raptor_fact_count",
                "config",
                name="watermark_kind",
            ),
            nullable=False,
        ),
        sa.Column("ref", sa.Text(), server_default="global", nullable=False),
        sa.Column("counter", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("payload", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scopes", "kind", "ref", name="uq_watermark_scope_kind_ref"),
    )
    op.create_index("ix_watermark_created_by", "watermark", ["created_by"])
    op.create_index("ix_watermark_scopes", "watermark", ["scopes"], postgresql_using="gin")

    # Security invoker keeps the live view subject to underlying RLS.
    op.execute(
        CreateView(
            live_fact_select(),
            "live_fact",
            postgresql_with={"security_invoker": True},
        )
    )

    for statement in bm25_lexical_statements():
        op.execute(statement)

    # Content-addressed object metadata shared by every artifact revision.
    op.create_table(
        "blob",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.Uuid(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("stored_size", sa.Integer(), nullable=False),
        sa.Column(
            "encoding",
            ENUM("identity", "zstd", name="blob_encoding"),
            server_default="identity",
            nullable=False,
        ),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("storage_version", sa.String(512), nullable=True),
        sa.Column("media_type", sa.String(255), nullable=True),
        sa.Column("etag", sa.String(512), nullable=True),
        sa.Column("integrity_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("integrity_error", sa.String(1024), nullable=True),
        sa.CheckConstraint("size >= 0", name="ck_blob_size_nonnegative"),
        sa.CheckConstraint("stored_size >= 0", name="ck_blob_stored_size_nonnegative"),
        sa.CheckConstraint("stored_size <= size", name="ck_blob_stored_size_bounded"),
        sa.CheckConstraint("storage_key <> ''", name="ck_blob_storage_key_nonempty"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_blob_content_hash_size", "blob", ["content_hash", "size"])
    op.create_index("ix_blob_integrity_checked_at", "blob", ["integrity_checked_at"])

    # Immutable artifacts and their bytes-backed revisions.
    op.create_table(
        "artifact",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("promoted_from", sa.Uuid(), nullable=True),
        sa.CheckConstraint("name <> ''", name="ck_artifact_name_nonempty"),
        sa.ForeignKeyConstraint(["promoted_from"], ["artifact.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("promoted_from", "scopes", name="uq_artifact_promotion_scope"),
        sa.UniqueConstraint("source_uri", "scopes", name="uq_artifact_source_scope"),
    )
    op.create_index("ix_artifact_created_by", "artifact", ["created_by"])
    op.create_index("ix_artifact_promoted_from", "artifact", ["promoted_from"])
    op.create_index("ix_artifact_scopes", "artifact", ["scopes"], postgresql_using="gin")

    op.create_table(
        "artifact_content",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "state",
            ENUM(
                "pending", "queued", "processing", "ready", "failed", name="artifact_content_state"
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("companion_text", sa.Text(), nullable=True),
        sa.Column("markdown", sa.Text(), nullable=True),
        sa.Column("docling_json", JSONB(), nullable=True),
        sa.Column("details", JSONB(), server_default="{}", nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("revision > 0", name="ck_artifact_content_revision_positive"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["blob_id"], ["blob.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_id", "revision", name="uq_artifact_content_revision"),
        # Bind each blob to at most one revision of an artifact and expose the composite key
        # the document pair foreign key points at.
        sa.UniqueConstraint("artifact_id", "blob_id", name="uq_artifact_content_blob"),
        sa.UniqueConstraint("artifact_id", "id", name="uq_artifact_content_artifact_id_id"),
    )
    op.create_index("ix_artifact_content_artifact_id", "artifact_content", ["artifact_id"])
    op.create_index("ix_artifact_content_blob_id", "artifact_content", ["blob_id"])
    op.create_index("ix_artifact_content_created_by", "artifact_content", ["created_by"])
    op.create_index(
        "ix_artifact_content_scopes", "artifact_content", ["scopes"], postgresql_using="gin"
    )
    op.create_index("ix_artifact_content_state", "artifact_content", ["state"])

    # Append-only usage ledger.
    op.create_table(
        "usage_event",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "operation",
            ENUM(
                "recall",
                "remember_text",
                "remember_file",
                "share",
                "artifact_read",
                name="usage_event_operation",
            ),
            nullable=False,
        ),
        sa.Column("targets", ARRAY(sa.Uuid()), nullable=False),
        sa.Column("request_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("response_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("items", sa.Integer(), server_default="1", nullable=False),
        sa.Column("duration_ms", sa.Float(), server_default="0.0", nullable=False),
        sa.CheckConstraint("request_bytes >= 0", name="ck_usage_request_bytes_nonnegative"),
        sa.CheckConstraint("response_bytes >= 0", name="ck_usage_response_bytes_nonnegative"),
        sa.CheckConstraint("items >= 0", name="ck_usage_items_nonnegative"),
        sa.CheckConstraint("duration_ms >= 0", name="ck_usage_duration_ms_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_usage_event_created_by", "usage_event", ["created_by"])
    op.create_index("ix_usage_event_operation", "usage_event", ["operation"])
    op.create_index("ix_usage_event_scopes", "usage_event", ["scopes"], postgresql_using="gin")
    op.create_index("ix_usage_event_targets", "usage_event", ["targets"], postgresql_using="gin")

    # Wire the document back to the artifact revision it was ingested from. The content link is a
    # composite pair so a document can only point at a revision that truly belongs to its artifact.
    op.create_foreign_key(
        "fk_document_artifact_id_artifact",
        "document",
        "artifact",
        ["artifact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_document_artifact_content_pair",
        "document",
        "artifact_content",
        ["artifact_id", "artifact_content_id"],
        ["artifact_id", "id"],
        ondelete="SET NULL",
    )

    # Durable single-use upload capability store shared by every AIZK process.
    op.create_table(
        "upload_capability",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("capability", sa.String(128), nullable=False),
        sa.Column("ticket", JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("capability"),
    )
    op.create_index("ix_upload_capability_created_by", "upload_capability", ["created_by"])
    op.create_index("ix_upload_capability_expires_at", "upload_capability", ["expires_at"])
    op.create_index(
        "ix_upload_capability_scopes", "upload_capability", ["scopes"], postgresql_using="gin"
    )

    for table in ("artifact", "artifact_content", "blob", "upload_capability", "usage_event"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {APP_ROLE}")

    # Force declared policies on scoped and content tables.
    for table, (mutable, deletable, read_through) in _SCOPED_TABLES.items():
        op.invoke(
            AlterRLSOp(
                table,
                before=None,
                after=scoped_rls(table, mutable, deletable, read_through),
            )
        )
    for table in _CONTENT_TABLES:
        op.invoke(AlterRLSOp(table, before=None, after=content_rls(table)))
    op.invoke(AlterRLSOp("blob", before=None, after=blob_rls()))
    op.invoke(AlterRLSOp("upload_capability", before=None, after=upload_capability_rls()))

    # Guard the mutable content table so a caller can only attach a blob it legitimately owns or
    # can already read, and so a committed `blob_id` can never be re-pointed at foreign bytes.
    op.execute(_ATTACHABLE_FUNCTION)
    op.execute(_GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER artifact_content_guard_blob "
        "BEFORE INSERT OR UPDATE ON artifact_content "
        "FOR EACH ROW EXECUTE FUNCTION artifact_content_guard_blob()"
    )


def downgrade() -> None:
    raise NotImplementedError("the squashed initial schema has no faithful reverse")
