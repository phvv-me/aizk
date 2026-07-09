# squashed initial schema at head: extensions, the live ontology catalog, the memory tables, the
# bi-temporal content/claim knowledge graph, communities, profiles, session working memory, and
# watermarks, every tenant-scoped table forced under the GUC-based scope-set visibility lattice and
# every content table under its own visible-through-a-claim policy. Identity lives in Logto: there
# is no local user, group, or membership table, a row's owner_id and scopes are uuid5 values
# derived from the verified token, and the row level security policies read the caller's standing
# from per-transaction GUCs rather than a membership join.
#
# Revision ID: 0001_init
# Revises:

import importlib.resources
from collections.abc import Sequence

import inflection
import sqlalchemy as sa
from jinja2 import Environment
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSTZRANGE, TSVECTOR

from aizk.config import settings
from aizk.store.mixins.view import create_view_ddl
from aizk.store.models.views.live_fact import LiveFact
from alembic import op

# ENTITY_KINDS/RELATION_KINDS seed the live ontology catalog entity_content.type/fact_content.
# predicate foreign-key against, (name, description, domain, structural). Frozen here rather
# than read from aizk.extract.ontology (which no longer even defines a fixed vocabulary), a
# migration is a historical record of what a fresh database looked like at this revision, never a
# view onto code that keeps evolving out from under it. Each name is stored in its canonical
# snake_case form (`canonical` below, the same fold `OntologyKind.canonical` applies to every
# write), so the human-readable PascalCase entity labels here land as `raptor_summary`,
# `code_artifact`, and so on, exactly the strings a content row's `type`/`predicate` stores.
ENTITY_KINDS: tuple[tuple[str, str, str, bool], ...] = (
    # core, structural, system-written, never extractor-emitted or deactivatable
    ("RaptorSummary", "A recursive summary tree node built above entity clusters.", "core", True),
    ("Observation", "A reflective insight the system derives from existing facts.", "core", True),
    # general, cross-cutting vocabulary no single domain owns
    (
        "Concept",
        "A catch-all for an idea or topic that fits no more specific type.",
        "general",
        False,
    ),
    # Project and Area are structural, declared-only types the extractor never emits, so a note is
    # a project or an area purely because its own #project or #area tag says so, and a roster of
    # either is exactly the notes that declared themselves rather than whatever a small model
    # over-tagged. The trust-declared-structure path in extract.journal writes them.
    (
        "Project",
        "A concrete effort with a start and an end that produces a result, the unit a projects "
        "rollup treats as a node and its member notes as parts.",
        "general",
        True,
    ),
    (
        "Area",
        "An ongoing domain of responsibility or identity with no end date, the container that "
        "holds projects and the notes that outlive any one of them.",
        "general",
        True,
    ),
    ("Tool", "A named library, framework, or piece of software.", "general", False),
    ("Person", "A specific individual.", "general", False),
    ("Decision", "A choice made and the reasoning behind it.", "general", False),
    ("Pattern", "A reusable approach or standing preference.", "general", False),
    ("Gotcha", "A trap or surprising behavior worth remembering.", "general", False),
    ("Goal", "An aim being worked toward.", "general", False),
    # coding
    ("Module", "A source code module or file.", "coding", False),
    ("Function", "A named function or method.", "coding", False),
    # research
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
    # finance
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
    # personal
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


def canonical(name: str) -> str:
    """Fold an ontology name to snake_case, the same rule `OntologyKind.canonical` applies.

    Inlined rather than imported from the app so this migration stays a frozen record of the seed
    it writes, immune to a later change in the model's own helper. `underscore` breaks CamelCase
    apart and `parameterize` folds spacing and punctuation into single underscores, idempotent on
    an already-canonical name so the snake_case relation predicates pass through unchanged.

    name: a raw catalog name, PascalCase, spaced, or already canonical.
    """
    return inflection.parameterize(inflection.underscore(name), separator="_")


# the big, backend-branching or fully static DDL this migration executes lives as .sql/.sql.j2
# files shipped inside the package (the migrations dir already ships in the wheel) rather than
# inline Python string-building, read back relative to this migrations package so the source stays
# readable as orchestration and the SQL stays readable as SQL. `live_fact` needs no file of its
# own anymore: `LiveFact.__view_select__` (`store.models.views.live_fact`) is that view's one
# source of truth, and `create_view_ddl`/`drop_view_ddl` (`store.mixins.view`) compile it straight
# into DDL, so only `hybrid_recall.sql.j2` still lives under `sql/`.
SQL_DIR = importlib.resources.files("aizk.store.migrations") / "sql"


def render_sql(name: str, **context: str | float) -> str:
    """A `.sql.j2` template rendered against the settings its DDL genuinely branches on.

    name: filename under `store/migrations/sql/`.
    context: the template's own variables, such as the selected bm25 backend.
    """
    return Environment().from_string((SQL_DIR / name).read_text()).render(**context)


revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# the halfvec width every embedding column is created at, 1024 for the default text lane
EMBED_DIM = settings.embed_dim

# which vector index the halfvec columns are built with and which lexical lane the chunks carry,
# vchordrq + vchord_bm25 by default and hnsw + tsvector the portable managed-Postgres fallback
INDEX_BACKEND = settings.index_backend
BM25_BACKEND = settings.bm25_backend

# vector and the small SQL helpers are always present, while the VectorChord index and bm25
# extensions are only created for the backends that use them, so a managed Postgres running the
# portable hnsw + tsvector fallback never hits a CREATE EXTENSION for an extension it lacks
BASE_EXTENSIONS = ("vector", "pg_trgm", "pgcrypto")

# the offline BERT tokenizer the vchord_bm25 lane tokenizes chunk text and queries with, preloaded
# in the vchord-suite image so no model bytes are ever fetched at migrate time
BM25_TOKENIZER = "aizk_bm25"
BM25_INDEX = "ix_chunk_bm25"

# the extension schemas the app role needs USAGE and read on so it can call tokenize and rank with
# the bm25_query operator, granted only when the vchord_bm25 lane is built
BM25_SCHEMAS = ("tokenizer_catalog", "bm25_catalog")

# the chunk-lane fusion's trusted-first floor, baked into the function body as a literal the way
# BM25_BACKEND already branches the lexical lane DDL, since a SQL-language function takes no
# config, only its five typed parameters
PROMOTED_BONUS = settings.promoted_bonus


def bm25_lexical_statements() -> list[str]:
    """The DDL that builds the vchord_bm25 lexical lane on the chunk table.

    Creates the offline tokenizer, adds the bm25vector column, keeps it in sync with chunk text
    through a before-write trigger the way the generated tsv column mirrors its text, then builds
    the bm25 index and grants the app role the schema usage and reads its query path needs.
    """
    return [
        f"SELECT tokenizer_catalog.create_tokenizer('{BM25_TOKENIZER}', $$\n"
        'model = "llmlingua2"\n$$)',
        "ALTER TABLE chunk ADD COLUMN bm25 bm25vector",
        # the trigger tokenizes the contextual `lexical` text when set and the raw span otherwise,
        # the coalesce mirroring the generated tsv column so both lexical lanes see the same field
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


def required_extensions(index_backend: str, bm25_backend: str) -> tuple[str, ...]:
    """The extensions this schema needs given the index and bm25 backends.

    The VectorChord index and bm25 extensions join the always-present base only when their backend
    is selected, so the portable fallback never creates an extension a managed Postgres lacks.

    index_backend: the selected vector index backend, vchordrq or hnsw.
    bm25_backend: the selected lexical backend, vchord_bm25 or tsvector.
    """
    extensions = list(BASE_EXTENSIONS)
    if index_backend == "vchordrq":
        extensions.append("vchord")
    if bm25_backend == "vchord_bm25":
        extensions.extend(("vchord_bm25", "pg_tokenizer"))
    return tuple(extensions)


def vector_index_ddl(name: str, table: str, backend: str) -> str:
    """The CREATE INDEX statement for one embedding column under the selected index backend.

    Both backends rank the halfvec column by the halfvec_cosine_ops opclass and the `<=>` operator,
    so only the access method the index is built with differs, vchordrq or hnsw.

    name: the index name, matching the ORM `embedding_index` so the two DDL sources agree.
    table: the table whose `embedding` column is indexed.
    backend: the access method, vchordrq or hnsw.
    """
    return f"CREATE INDEX {name} ON {table} USING {backend} (embedding halfvec_cosine_ops)"


# every tenant-scoped table carries owner_id and scopes and is forced under the scope policies
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

# every content table carries no owner or scope of its own, forced under its own custom
# visible-through-a-claim, freely-mintable, immutable policy set instead of the scope policies
CONTENT_TABLES = ("entity_content", "fact_content")

# the restricted, non-superuser, non-bypassrls login role the app connects as, so row level
# security is always enforced; the owner role keeps running migrations. Created by
# `initdb/roles.sql` against a fresh volume, not by this migration; named here only for the
# per-table grants and bm25-schema grants this migration still makes directly.
APP_ROLE = "aizk_app"


def upgrade() -> None:
    for extension in required_extensions(INDEX_BACKEND, BM25_BACKEND):
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")

    # documents and their chunks, each scoped by owner_id and a shared group scope-set; chunk text
    # carries both a halfvec embedding for dense search and a generated tsvector for lexical search
    op.create_table(
        "document",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("source_uri", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("promoted_from", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["promoted_from"], ["document.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_uri"),
    )
    op.create_index("ix_document_content_hash", "document", ["content_hash"])
    op.create_index("ix_document_owner_id", "document", ["owner_id"])
    op.create_index("ix_document_scopes", "document", ["scopes"], postgresql_using="gin")

    op.create_table(
        "chunk",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("lexical", sa.Text(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        # null until the graph build has run extraction and consolidation over this chunk at least
        # once, regardless of whether that pass minted any claim; `pending_chunks` filters on this
        # directly instead of an anti-join against fact_claim, since a chunk whose prose asserts no
        # fact worth keeping still finished a real pass and must never be re-extracted forever.
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        # the generated lexical vector reads the contextual `lexical` text when an ingest filled it
        # and falls back to the raw span otherwise, so the full-text lane matches on the situating
        # preamble without it ever reaching the dense embedding or the displayed chunk text
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('english', coalesce(lexical, text))", persisted=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # indexed: promote's document-ordered rebuild and build_graph's source-title filter both
    # reverse-look-up a document's chunks by this column; EXPLAIN against a seeded corpus showed
    # the unindexed lookup falling back to a full scan of the chunk table
    op.create_index("ix_chunk_document_id", "chunk", ["document_id"])
    op.execute(vector_index_ddl("ix_chunk_embedding", "chunk", INDEX_BACKEND))
    op.create_index("ix_chunk_owner_id", "chunk", ["owner_id"])
    # pending_chunks reads exactly this predicate every build_graph and enqueue_pending run; the
    # partial index only ever covers the still-unprocessed rows, so it shrinks as a build drains
    # rather than growing with the corpus the way a plain index on the column would.
    op.create_index(
        "ix_chunk_pending", "chunk", ["id"], postgresql_where=sa.text("processed_at IS NULL")
    )
    # scopes earns its own GIN index here, unlike most scoped tables, since promotion copies and
    # RLS reads both filter chunks by target scope-set often
    op.create_index("ix_chunk_scopes", "chunk", ["scopes"], postgresql_using="gin")
    op.create_index("ix_chunk_tsv", "chunk", ["tsv"], postgresql_using="gin")

    # the live ontology catalog entity_content.type/fact_content.predicate foreign-key against,
    # ENTITY_KINDS/RELATION_KINDS above seeding every kind this revision ships with. Growing the
    # vocabulary from here on is an ordinary row insert, never a schema migration, the extraction
    # pipeline's auto-create cascade minting a fresh row. The catalog only grows, never deletes,
    # so it carries no active or origin state, an auto-created row is simply tagged domain="auto".
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
        sa.PrimaryKeyConstraint("name"),
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
                "name": canonical(name),
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
    )
    op.bulk_insert(
        relation_kind_table,
        [
            {
                "name": canonical(name),
                "description": description,
                "domain": domain,
                "structural": structural,
            }
            for name, description, domain, structural in RELATION_KINDS
        ],
    )

    # the bi-temporal knowledge graph, content-addressed content deduplicated across every tenant
    # plus each container's own per-tenant claim on it: entity_content is the immutable node
    # identity (name, type, embedding) two owners extracting the same thing land on together, and
    # entity_claim is one container's stake in it, owner_id/scopes/attributes/created_at, the
    # per-tenant row a fact's subject or object resolves against.
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

    op.create_table(
        "entity_claim",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content_id", sa.Uuid(), nullable=False),
        sa.Column("attributes", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(["content_id"], ["entity_content.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_id", "owner_id", "scopes", name="uq_entity_claim_content_owner_scope"
        ),
    )
    op.create_index("ix_entity_claim_content_id", "entity_claim", ["content_id"])
    op.create_index("ix_entity_claim_owner_id", "entity_claim", ["owner_id"])
    op.create_index("ix_entity_claim_scopes", "entity_claim", ["scopes"], postgresql_using="gin")

    # fact_content is the immutable edge structure (subject, predicate, object, statement,
    # embedding) two owners extracting the identical fact land on together, the exact collision the
    # content/claim split fixes on the edge table the same way it fixes the node table above; every
    # bi-temporal, curation, and decay column lives on fact_claim instead, since that state is
    # inherently a container's own, never structural.
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
        sa.Column("owner_id", sa.Uuid(), nullable=False),
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
        sa.Column("source_chunk_id", sa.Uuid(), nullable=True),
        sa.Column("promoted_from", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["content_id"], ["fact_content.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["promoted_from"], ["fact_claim.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_chunk_id"], ["chunk.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fact_claim_content_id", "fact_claim", ["content_id"])
    op.create_index("ix_fact_claim_owner_id", "fact_claim", ["owner_id"])
    # pending_chunks's NOT EXISTS anti-join reads every chunk against this column every build_graph
    # and enqueue_pending run; EXPLAIN against a seeded corpus showed the unindexed join
    # materializing the whole claim table per candidate chunk, a chunks-times-claims cost
    op.create_index("ix_fact_claim_source_chunk_id", "fact_claim", ["source_chunk_id"])
    op.create_index("ix_fact_claim_promoted_from", "fact_claim", ["promoted_from"])
    op.create_index("ix_fact_claim_scopes", "fact_claim", ["scopes"], postgresql_using="gin")
    # GiST for `valid` and `recorded`, the containment (`@>`) operator the as-of gate filters on.
    # `upper_inf` is a function over the range, not an indexable range_ops operator, so neither
    # plain index makes the live gate sargable alone; ix_fact_claim_live is `valid` scoped by its
    # own `upper_inf(recorded)` partial predicate, so a query filtering both matches the partial
    # index and scans only the live set. uq_fact_claim_live is the one-live-claim-per-container-
    # per-content moat: a partial unique index since Postgres allows no WHERE clause on a table
    # constraint, only on an index; a `uuid[]` carries no NULL to fold, an empty array is its own
    # ordinary, comparable value, so this needs no NULLS NOT DISTINCT the way an old scalar `scope`
    # column once did.
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
        ["content_id", "owner_id", "scopes"],
        unique=True,
        postgresql_where=sa.text("upper_inf(recorded)"),
    )

    # community summaries over the entity graph
    op.create_table(
        "community",
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
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
    op.create_index("ix_community_owner_id", "community", ["owner_id"])

    # entity profiles, each a running portrait of one subject entity content
    op.create_table(
        "profile",
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["subject_id"], ["entity_content.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "subject_id", name="uq_profile_owner_subject"),
    )
    op.execute(vector_index_ddl("ix_profile_embedding", "profile", INDEX_BACKEND))
    op.create_index("ix_profile_owner_id", "profile", ["owner_id"])
    op.create_index("ix_profile_subject_id", "profile", ["subject_id"])

    # session (working) memory: one embedded row per remembered item, the cheap front tier a
    # remember writes to immediately, scoped and forced like the graph it is later promoted into.
    # promoted_at stamps when an item's knowledge reached the long-term graph so it leaves the set.
    op.create_table(
        "session_item",
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(vector_index_ddl("ix_session_item_embedding", "session_item", INDEX_BACKEND))
    op.create_index("ix_session_item_owner_id", "session_item", ["owner_id"])
    op.create_index("ix_session_item_promoted_at", "session_item", ["promoted_at"])

    # one tiny counter row per owner, kind, and ref the autonomous engine debounces its passes on,
    # scoped and forced exactly like the memory it tracks so a count never leaks across users
    op.create_table(
        "watermark",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Uuid()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "entity_dirty",
                "fact_count",
                "raptor_fact_count",
                "curation_pending",
                "scorecard",
                "config",
                name="watermark_kind",
            ),
            nullable=False,
        ),
        sa.Column("ref", sa.Text(), server_default="global", nullable=False),
        sa.Column("counter", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("payload", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "kind", "ref", name="uq_watermark_owner_kind_ref"),
    )
    op.create_index("ix_watermark_owner_id", "watermark", ["owner_id"])

    # the restricted app role itself, `initdb/roles.sql`, is provisioned once against a fresh
    # volume before any migration ever connects (mounted at /docker-entrypoint-initdb.d/), owns
    # NOSUPERUSER NOBYPASSRLS standing, schema usage, and the default privileges that hand it CRUD
    # on every table and sequence a migration creates from here on, so this migration never creates
    # the role or grants it schema-wide access itself; `apply_scoped_rls` below still grants each
    # table explicitly, a harmless belt over the default privilege.

    # live_fact narrows the fact_claim x fact_content join to exactly the live version,
    # `FactClaim.is_current`'s own predicate rendered once as a view, `LiveFact.__view_select__`'s
    # single source of truth for both the mapped class and this DDL (`store.mixins.view`); the DDL
    # itself, `security_invoker = true` load-bearing since SQLAlchemy 2.1.0b3's CreateView compiler
    # has no such path and a default view would silently bypass row level security.
    op.execute(create_view_ddl(LiveFact.__tablename__, LiveFact.__view_select__()))

    # the vchord_bm25 lexical lane, built only for that backend so the portable tsvector fallback
    # leaves the chunk table with just its generated tsv column.
    if BM25_BACKEND == "vchord_bm25":
        for statement in bm25_lexical_statements():
            op.execute(statement)

    # the one-statement hybrid fusion, following live_fact and the lexical lane it reads through;
    # a plain `language sql` function is invoker-rights by default, the same story
    # security_invoker spells out explicitly for the view above, so it needs no grant beyond
    # Postgres's own default of EXECUTE to PUBLIC on a newly created function. The DDL,
    # backend-branched on the lexical CTE and the promoted-bonus literal, lives in
    # `migrations/sql/hybrid_recall.sql.j2`, unchanged by the content/claim split since it reads
    # `live_fact` by column name alone and every column it names still exists on this view.
    op.execute(
        render_sql(
            "hybrid_recall.sql.j2",
            bm25_backend=BM25_BACKEND,
            promoted_bonus=PROMOTED_BONUS,
            bm25_index=BM25_INDEX,
            bm25_tokenizer=BM25_TOKENIZER,
        )
    )

    # force every declared policy on each tenant-scoped table, so even the table owner is subject
    # to them and no row leaks across users. The content tables carry no owner_id/scope of their
    # own, so their custom visible-through-a-claim policy set is applied the identical way through
    # the same op, which reads whatever `__rls_policies__` the table's model declared rather than
    # assuming the four default scope policies.
    for table in (*SCOPED_TABLES, *CONTENT_TABLES):
        op.apply_scoped_rls(table)


def downgrade() -> None:
    raise NotImplementedError("the squashed initial schema has no faithful reverse")
