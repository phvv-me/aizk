# squashed initial schema at head: extensions, the memory tables, the visibility lattice, the
# restricted app role, the bi-temporal content/claim graph, communities, profiles, api keys, admin
# flag, and watermarks, all with forced row level security on every tenant-scoped table and every
# content table's own visible-through-a-claim policy
#
# Revision ID: 0001_init
# Revises:

import importlib.resources
from collections.abc import Sequence

import sqlalchemy as sa
from jinja2 import Environment
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE

from aizk.config import settings
from aizk.extract.ontology import EntityType, RelationType, check_in_sql
from alembic import op

# the big, backend-branching or fully static DDL this migration executes lives as .sql/.sql.j2
# files shipped inside the package (the migrations dir already ships in the wheel) rather than
# inline Python string-building, read back relative to this migrations package so the source stays
# readable as orchestration and the SQL stays readable as SQL.
SQL_DIR = importlib.resources.files("aizk.store.migrations") / "sql"


def read_sql(name: str) -> str:
    """A static `.sql` file's text, unchanged by any migration setting.

    name: filename under `store/migrations/sql/`.
    """
    return (SQL_DIR / name).read_text()


def render_sql(name: str, **context: str | float) -> str:
    """A `.sql.j2` template rendered against the settings its DDL genuinely branches on.

    name: filename under `store/migrations/sql/`.
    context: the template's own variables, such as the selected bm25 backend.
    """
    return Environment().from_string((SQL_DIR / name).read_text()).render(**context)


# the halfvec width every embedding column is created at, 1024 for the default text lane
EMBED_DIM = settings.embed_dim

# which vector index the halfvec columns are built with and which lexical lane the chunks carry,
# vchordrq + vchord_bm25 by default and hnsw + tsvector the portable managed-Postgres fallback
INDEX_BACKEND = settings.index_backend
BM25_BACKEND = settings.bm25_backend

revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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


# hybrid_recall()'s two signature spellings: DROP FUNCTION identifies an overload by its bare
# parameter types alone, while CREATE FUNCTION names and types each one, so the same five
# parameters are written once each way rather than derived from one another.
HYBRID_RECALL_TYPES = "halfvec, text, int, int, int"
HYBRID_RECALL_PARAMS = "qvec halfvec, qtext text, k int, rrf_k int, fusion_depth int"

# the chunk-lane fusion's trusted-first floor, baked into the function body as a literal the way
# BM25_BACKEND already branches the lexical lane DDL, since a SQL-language function takes no
# config, only its five typed parameters
PROMOTED_BONUS = settings.promoted_bonus


# every tenant-scoped table carries owner_id and scope and is forced under the scope policies
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

# the well-known identity that owns any pre-lattice row and always administers the engine, so a
# fresh single-user stack self-administers from the first migration
SYSTEM_PRINCIPAL_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    for extension in required_extensions(INDEX_BACKEND, BM25_BACKEND):
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")

    # the visibility lattice the scope policies read: principals own rows, group_ gathers them, and
    # memberships bridge a principal into a group's shared scope. group_ carries the trailing
    # underscore `TableBase.__tablename__` appends on any reserved-word collision, since GROUP is a
    # reserved SQL keyword; every other table's id is client-generated by IdMixin so no table here
    # needs a server-side uuid default either.
    op.create_table(
        "principal",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("zitadel_sub", sa.Text(), nullable=True, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "group_",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("public", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("curated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "membership",
        sa.Column(
            "principal_id",
            sa.Uuid(),
            sa.ForeignKey("principal.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "group_id",
            sa.Uuid(),
            sa.ForeignKey("group_.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.Enum("reader", "writer", "admin", name="membership_role"),
            nullable=False,
            server_default="writer",
        ),
        sa.PrimaryKeyConstraint("principal_id", "group_id"),
    )

    # the system principal owns any row ingested before a caller is known and is an admin from the
    # start, so the auth layer and the pre-lattice backfill both have an identity to point at.
    # bulk_insert over a literal INSERT so the seeded value is parameter-bound rather than
    # string-formatted into the statement text.
    principal_table = sa.table(
        "principal",
        sa.column("id", sa.Uuid()),
        sa.column("display_name", sa.Text()),
        sa.column("is_admin", sa.Boolean()),
    )
    op.bulk_insert(
        principal_table,
        [
            {
                "id": SYSTEM_PRINCIPAL_ID,
                "display_name": "system",
                "is_admin": True,
            }
        ],
    )

    # documents and their chunks, each scoped by owner_id and shared group scope; the owner_id and
    # scope foreign keys keep their explicit fk_<table>_<column> names, and chunk text carries both
    # a halfvec embedding for dense search and a generated tsvector for lexical search
    op.create_table(
        "document",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False, server_default="note"),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("source_uri", sa.String(), nullable=True, unique=True),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.Uuid(), nullable=True),
        sa.Column("promoted_from", sa.Uuid(), sa.ForeignKey("document.id"), nullable=True),
    )
    op.create_foreign_key("fk_document_owner_id", "document", "principal", ["owner_id"], ["id"])
    op.create_foreign_key(
        "fk_document_scope", "document", "group_", ["scope"], ["id"], ondelete="SET NULL"
    )

    op.create_table(
        "chunk",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("document.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("lexical", sa.Text(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
    )
    # the generated lexical vector reads the contextual `lexical` text when an ingest filled it and
    # falls back to the raw span otherwise, so the full-text lane matches on the situating preamble
    # without the preamble ever reaching the dense embedding or the displayed chunk text
    op.execute(
        "ALTER TABLE chunk ADD COLUMN tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', coalesce(lexical, text))) STORED"
    )
    op.add_column("chunk", sa.Column("owner_id", sa.Uuid(), nullable=False))
    op.add_column("chunk", sa.Column("scope", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_chunk_owner_id", "chunk", "principal", ["owner_id"], ["id"])
    op.create_foreign_key(
        "fk_chunk_scope", "chunk", "group_", ["scope"], ["id"], ondelete="SET NULL"
    )

    # the bi-temporal knowledge graph, content-addressed content deduplicated across every tenant
    # plus each container's own per-tenant claim on it: entity_content is the immutable node
    # identity (name, type, embedding) two owners extracting the same thing land on together, and
    # entity_claim is one container's stake in it, owner_id/scope/attributes/created_at, the
    # per-tenant row a fact's subject or object resolves against.
    op.create_table(
        "entity_content",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        sa.CheckConstraint(check_in_sql("type", EntityType), name="ck_entity_content_type"),
    )
    op.create_table(
        "entity_claim",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "content_id",
            sa.Uuid(),
            sa.ForeignKey("entity_content.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("principal.id"), nullable=False),
        sa.Column(
            "scope",
            sa.Uuid(),
            sa.ForeignKey("group_.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "attributes",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint(
        "uq_entity_claim_content_owner_scope",
        "entity_claim",
        ["content_id", "owner_id", "scope"],
        postgresql_nulls_not_distinct=True,
    )

    # fact_content is the immutable edge structure (subject, predicate, object, statement,
    # embedding) two owners extracting the identical fact land on together, the exact collision the
    # content/claim split fixes on the edge table the same way it fixes the node table above; every
    # bi-temporal, curation, and decay column that lived on the old single fact table now lives on
    # fact_claim instead, since that state is inherently a container's own, never structural.
    op.create_table(
        "fact_content",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "subject_id",
            sa.Uuid(),
            sa.ForeignKey("entity_content.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "object_id",
            sa.Uuid(),
            sa.ForeignKey("entity_content.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        sa.CheckConstraint(
            check_in_sql("predicate", RelationType), name="ck_fact_content_predicate"
        ),
    )
    op.create_table(
        "fact_claim",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "content_id",
            sa.Uuid(),
            sa.ForeignKey("fact_content.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("principal.id"), nullable=False),
        sa.Column(
            "scope",
            sa.Uuid(),
            sa.ForeignKey("group_.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("valid", TSTZRANGE(), nullable=True),
        sa.Column(
            "recorded",
            TSTZRANGE(),
            nullable=False,
            server_default=sa.text("tstzrange(now(), NULL, '[)')"),
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_accessed", sa.DateTime(timezone=True), nullable=True),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "attributes",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "source_chunk_id",
            sa.Uuid(),
            sa.ForeignKey("chunk.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "promoted_from",
            sa.Uuid(),
            sa.ForeignKey("fact_claim.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # community summaries over the entity graph
    op.create_table(
        "community",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("principal.id"), nullable=False),
        sa.Column(
            "scope",
            sa.Uuid(),
            sa.ForeignKey("group_.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        sa.Column(
            "member_ids",
            sa.ARRAY(sa.Uuid()),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # entity profiles, each a running portrait of one subject entity content
    op.create_table(
        "profile",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("principal.id"), nullable=False),
        sa.Column(
            "scope",
            sa.Uuid(),
            sa.ForeignKey("group_.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "subject_id",
            sa.Uuid(),
            sa.ForeignKey("entity_content.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("owner_id", "subject_id", name="uq_profile_owner_subject"),
    )

    # session (working) memory: one embedded row per remembered item, the cheap front tier a
    # remember writes to immediately, scoped and forced like the graph it is later promoted into.
    # promoted_at stamps when an item's knowledge reached the long-term graph so it leaves the set.
    op.create_table(
        "session_item",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("principal.id"), nullable=False),
        sa.Column(
            "scope",
            sa.Uuid(),
            sa.ForeignKey("group_.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(), nullable=False, server_default="note"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", HALFVEC(EMBED_DIM), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # one tiny counter row per owner, kind, and ref the autonomous engine debounces its passes on,
    # scoped and forced exactly like the memory it tracks so a count never leaks across principals
    op.create_table(
        "watermark",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("principal.id"), nullable=False),
        sa.Column(
            "scope",
            sa.Uuid(),
            sa.ForeignKey("group_.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
        sa.Column("ref", sa.Text(), nullable=False, server_default="global"),
        sa.Column("counter", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("owner_id", "kind", "ref", name="uq_watermark_owner_kind_ref"),
    )

    # the dense ann lane on every embedded table, built with the configured index backend so this
    # DDL and the ORM `embedding_index` name the same access method and never drift
    op.execute(vector_index_ddl("ix_chunk_embedding", "chunk", INDEX_BACKEND))
    # the generated tsv column and its gin index stay for both bm25 backends so a full Chunk read
    # never misses a column, while the vchord_bm25 lane below adds its own bm25vector alongside
    op.execute("CREATE INDEX ix_chunk_tsv ON chunk USING gin (tsv)")
    op.create_index("ix_document_content_hash", "document", ["content_hash"])
    op.create_index("ix_chunk_owner_id", "chunk", ["owner_id"])
    op.create_index("ix_chunk_scope", "chunk", ["scope"])
    # promote's document-ordered rebuild and build_graph's source-title filter both reverse-look-up
    # a document's chunks by this column; EXPLAIN against a seeded corpus showed the unindexed
    # lookup falling back to a full scan of the chunk table
    op.create_index("ix_chunk_document_id", "chunk", ["document_id"])
    op.create_index("ix_document_owner_id", "document", ["owner_id"])
    op.create_index("ix_document_scope", "document", ["scope"])
    op.execute(vector_index_ddl("ix_entity_content_embedding", "entity_content", INDEX_BACKEND))
    op.execute(vector_index_ddl("ix_fact_content_embedding", "fact_content", INDEX_BACKEND))
    op.create_index("ix_entity_claim_content_id", "entity_claim", ["content_id"])
    op.create_index("ix_entity_claim_owner_id", "entity_claim", ["owner_id"])
    op.create_index("ix_fact_content_subject_id", "fact_content", ["subject_id"])
    op.create_index("ix_fact_content_object_id", "fact_content", ["object_id"])
    op.create_index("ix_fact_claim_content_id", "fact_claim", ["content_id"])
    op.create_index("ix_fact_claim_owner_id", "fact_claim", ["owner_id"])
    op.create_index("ix_fact_claim_promoted_from", "fact_claim", ["promoted_from"])
    # pending_chunks's NOT EXISTS anti-join reads every chunk against this column every build_graph
    # and enqueue_pending run; EXPLAIN against a seeded corpus showed the unindexed join
    # materializing the whole claim table per candidate chunk, a chunks-times-claims cost
    op.create_index("ix_fact_claim_source_chunk_id", "fact_claim", ["source_chunk_id"])
    # GiST for `valid`, the containment (`@>`) operator the as-of gate filters on; GiST again for
    # `recorded` since the as-of replay containment-tests it the same way. `upper_inf` is a
    # function over the range rather than a range_ops operator, so it is not sargable by either
    # plain index; ix_fact_claim_live is a GiST index over `valid` partial to exactly the live rows
    # (its own `upper_inf(recorded)` predicate), so a query filtering `upper_inf(recorded)` alone,
    # the record_access and decay UPDATEs among them, scans the live set instead of the full
    # history-included table. uq_fact_claim_live is the one-live-claim-per-container-per-content
    # moat, a partial unique index since Postgres allows no WHERE clause on a plain constraint.
    op.execute("CREATE INDEX ix_fact_claim_valid ON fact_claim USING gist (valid)")
    op.execute("CREATE INDEX ix_fact_claim_recorded ON fact_claim USING gist (recorded)")
    op.execute(
        "CREATE INDEX ix_fact_claim_live ON fact_claim USING gist (valid) "
        "WHERE upper_inf(recorded)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_fact_claim_live ON fact_claim (content_id, owner_id, scope) "
        "NULLS NOT DISTINCT WHERE upper_inf(recorded)"
    )
    op.execute(vector_index_ddl("ix_community_embedding", "community", INDEX_BACKEND))
    op.create_index("ix_community_owner_id", "community", ["owner_id"])
    op.execute(vector_index_ddl("ix_profile_embedding", "profile", INDEX_BACKEND))
    op.create_index("ix_profile_owner_id", "profile", ["owner_id"])
    op.create_index("ix_profile_subject_id", "profile", ["subject_id"])
    op.execute(vector_index_ddl("ix_session_item_embedding", "session_item", INDEX_BACKEND))
    op.create_index("ix_session_item_owner_id", "session_item", ["owner_id"])
    op.create_index("ix_session_item_promoted_at", "session_item", ["promoted_at"])
    op.create_index("ix_watermark_owner_id", "watermark", ["owner_id"])

    # the restricted app role itself, `initdb/roles.sql`, is provisioned once against a fresh
    # volume before any migration ever connects (mounted at /docker-entrypoint-initdb.d/), owns
    # NOSUPERUSER NOBYPASSRLS standing, schema usage, and the default privileges that hand it CRUD
    # on every table and sequence a migration creates from here on, so this migration never creates
    # the role or grants it schema-wide access itself; `apply_scoped_rls`/`apply_content_rls` below
    # still grant each table explicitly, a harmless belt over the default privilege.

    # live_fact narrows the fact_claim x fact_content join to exactly the live version,
    # `FactClaim.is_current`'s own predicate rendered once as a view rather than re-derived by hand
    # at every read site; the DDL itself, `security_invoker = true` load-bearing since SQLAlchemy
    # 2.1.0b3's CreateView compiler has no such path and a default view would silently bypass row
    # level security, lives in `migrations/sql/live_fact.sql`, static text with no backend branch.
    op.execute(read_sql("live_fact.sql"))

    # the vchord_bm25 lexical lane, built only for that backend so the portable tsvector fallback
    # leaves the chunk table with just its generated tsv column.
    if BM25_BACKEND == "vchord_bm25":
        for statement in bm25_lexical_statements():
            op.execute(statement)

    # the one-statement hybrid fusion, following live_fact and the lexical lane it reads through;
    # a plain `language sql` function is invoker-rights by default, the same story
    # security_invoker spells out explicitly for the view above, so it needs no grant beyond
    # Postgres's own default of EXECUTE to PUBLIC on a newly created function. The DDL, backend-
    # branched on the lexical CTE and the promoted-bonus literal, lives in
    # `migrations/sql/hybrid_recall.sql.j2`, unchanged by the content/claim split since it reads
    # `live_fact` by column name alone and every column it names still exists on the v2 view.
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
    # to them and no row leaks across principals; `fact_claim`'s own curation-admin escape rides
    # along automatically here, declared on the model itself rather than applied as a separate
    # step. The content tables carry no owner_id/scope of their own, so `apply_scoped_rls` never
    # runs against them; their custom visible-through-a-claim policy set is applied the identical
    # way through the same op, which reads whatever `__rls_policies__` the table's model declared
    # rather than assuming the four default scope policies.
    for table in (*SCOPED_TABLES, *CONTENT_TABLES):
        op.apply_scoped_rls(table)


def downgrade() -> None:
    # dropped first, mirroring how each was created last to first: hybrid_recall reads both
    # live_fact and, on the vchord_bm25 backend, the tokenizer and index the lane below tears
    # down, so it must be gone before either of them and before anything else touches fact_claim
    op.execute(f"DROP FUNCTION IF EXISTS hybrid_recall({HYBRID_RECALL_TYPES})")
    op.execute("DROP VIEW IF EXISTS live_fact")

    # drop the vchord_bm25 lane's standalone objects the table drop below does not reach, the
    # tokenizer catalog entry and the trigger function the dropped chunk trigger left behind, and
    # revoke the bm25 schema grants first so the app role carries no dependent privilege when it is
    # dropped below, the mirror of the grants bm25_lexical_statements handed it on the way up
    if BM25_BACKEND == "vchord_bm25":
        op.execute("DROP FUNCTION IF EXISTS chunk_bm25_sync() CASCADE")
        op.execute(f"SELECT tokenizer_catalog.drop_tokenizer('{BM25_TOKENIZER}')")
        for schema in BM25_SCHEMAS:
            op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {APP_ROLE}")
        op.execute(f"REVOKE USAGE ON SCHEMA {', '.join(BM25_SCHEMAS)} FROM {APP_ROLE}")

    # each protected table's declared policies, `fact_claim`'s curation-admin escape and every
    # content table's custom set included, since each rides along inside its own model's declared
    # set rather than a separate apply step
    for table in (*SCOPED_TABLES, *CONTENT_TABLES):
        op.drop_scoped_rls(table)

    # the app role itself, its schema usage, and its default privileges are `initdb/roles.sql`'s
    # responsibility, provisioned once against a fresh volume rather than by this migration, so
    # nothing here reverses them; only `docker compose down -v` tears the role down.

    op.drop_table("watermark")
    op.drop_table("session_item")
    op.drop_table("profile")
    op.drop_table("community")
    op.drop_table("fact_claim")
    op.drop_table("fact_content")
    op.drop_table("entity_claim")
    op.drop_table("entity_content")
    op.drop_table("chunk")
    op.drop_table("document")
    op.drop_table("membership")
    op.drop_table("group_")
    op.drop_table("principal")

    # native enum types outlive the table whose column referenced them, so each is dropped
    # explicitly once the last column using it is gone
    op.execute("DROP TYPE watermark_kind")
    op.execute("DROP TYPE membership_role")
