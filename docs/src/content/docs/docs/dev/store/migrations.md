---
title: "Migrations and DDL"
description: "The PostgreSQL revision history, fused CockroachDB baseline, declarative DDL and drift checks."
---

The profiles have separate Alembic histories. PostgreSQL has fourteen revisions in
`migrations/versions/`. CockroachDB has one baseline in
`migrations/cockroachdb/versions/`. Read
[Row level security](/docs/dev/store/rls/) first because policy is frozen into both baselines.

## PostgreSQL revision history

`0001_init` squashes the first historical chain. Later revisions add portable SQL, runtime,
quotas, web egress, storage controls, diagnostics, operator views and captions. Fresh PostgreSQL
installs replay the chain to head.

The initial revision carries PostgreSQL-specific extensions, BM25, half-vector indexes and the
first frozen policy set. Later revisions must not rewrite that history.

## CockroachDB fused baseline

`0001_cockroachdb` creates the current CockroachDB schema in one pass. It does not replay
PostgreSQL extension operations or queue history. It creates full vector columns, portable
durable queue tables, CockroachDB policy expressions and the private C-SPANN projection.

The baseline is fused because the cloud profile was introduced after the memory model existed.
Replaying backend-specific PostgreSQL history would add compatibility work with no deployed
CockroachDB state to preserve. Append a new CockroachDB revision only after a deployed cloud
database needs history beyond the baseline.

## What 0001_init lays down

```text
  extensions ─▶ tables and indexes ─▶ ontology seed ─▶ live_fact view
                                                            │
                          bm25 lexical lane ◀───────────────┘
                                  │
                          artifact side, grants
                                  │
                    frozen RLS ─▶ blob guard trigger
```

Order matters. The view is created before `chunk` gains its BM25 column, and policies are forced
only after every table they reference exists.

**Extensions.** `required_extensions()` returns `vector`, `pg_trgm`, `pgcrypto`, `vchord_bm25`
and `pg_tokenizer`, plus `vchord` when `settings.index_backend` is `vchordrq`, each created with
`CREATE EXTENSION IF NOT EXISTS`.

**The seeded ontology.** The revision carries its own copies of `ENTITY_KINDS` and
`RELATION_KINDS` rather than importing them from application code, so it keeps meaning after the
code moves on. It seeds **44 entity kinds** across six domains and **25 relation predicates**,
stored through `inflection.underscore` so `RaptorSummary` becomes `raptor_summary`. A small
`_RELATION_POLICIES` map assigns the non-default policies, `state` for `has_status` and `event`
for `observes` and `supersedes`, and everything else seeds as `set`.

**Vector indexes.** `vector_index_ddl()` renders the `halfvec_cosine_ops` index and runs for six
embedded tables, `chunk`, `entity_content`, `fact_content`, `community`, `profile` and
`session_item`. The backend and the embedding dimension are read from settings once and frozen
into the revision.

**The BM25 lexical lane.** `bm25_lexical_statements()` is the only place the lexical column
exists. It creates the `aizk_bm25` tokenizer, adds `chunk.bm25 bm25vector`, attaches the
`chunk_bm25_sync` trigger that tokenizes `coalesce(NEW.lexical, NEW.text)`, builds `ix_chunk_bm25`,
and grants the app role usage on the tokenizer schemas. None of this appears on the `Chunk` model.

**Frozen row security.** The revision does not call the mixins. It carries its own
`scoped_rls`, `content_rls`, `blob_rls` and `upload_capability_rls` functions and a
`_SCOPED_TABLES` map of eleven tables to their `(mutable, deletable, read_through)` triple, each
applied through `AlterRLSOp`. The duplication is the point. If a mixin predicate changes tomorrow,
the migration still builds the schema that existed when it was written, and the drift check tells
you the two have parted.

**The view.** `live_fact_select()` likewise rebuilds the defining select against literal
`sa.table` handles rather than importing `LiveFact`, then passes it to `CreateView` with
`security_invoker` on.

**The blob guard.** Two `plpgsql` functions and a trigger close the last hole.
`artifact_content_blob_attachable` is `SECURITY DEFINER`, so it sees the true global set of blob
references and allows an attach only when the blob is brand new or already reachable through a
revision the caller can read. `artifact_content_guard_blob` calls it on insert and rejects any
update that changes `blob_id`.

## The declarative DDL layer

`src/aizk/store/ddl/` is four small typed elements plus one compiler module. `CreateExtension`
renders the idempotent create. `Grant` pairs with `GrantTarget`, a `StrEnum` whose members are the
SQL templates themselves, so the dialect preparer quotes identifiers rather than string
formatting. `CreateView` backports PostgreSQL view options onto SQLAlchemy 2.1's native
`CreateView`, with a `FIXME` to delete once upstream issue 13432 lands. `postgresql_sql()`
compiles any of them to text for an external driver.

`ViewBase` in `src/aizk/store/mixins/view.py` is what makes a view a first-class model. A
subclass declares typed fields and a `__view_select__` classmethod, and
`__pydantic_init_subclass__` does the rest. It builds the `CreateView`
with `security_invoker` on, registers the view name in `metadata.info["views"]`, maps the class
imperatively, and sets `__rls__ = rls.Open()`, because a security-invoker view carries no policies
of its own and the base tables' forced row security governs every read through it. A security
**barrier** was rejected on purpose, since a barrier would stop the planner from pushing
vector-distance ordering into the content indexes.

## The zero-drift check

Autogenerate must come back empty against a migrated database, which takes deliberate exclusions,
all of them in `src/aizk/store/migrations/env.py`.

| Skipped | Why |
|---|---|
| tables starting with `pgqueuer` | owned by the queue, not by our metadata |
| every name in `metadata.info["views"]` | mapped as models, created as views |
| the reflected `chunk.bm25` column | exists only in the migration |
| `ix_chunk_bm25`, `ix_entity_content_name_lower`, `ix_entity_content_name_trgm` | expression and BM25 indexes written by hand |
| extension-owned tables | found by joining `pg_depend` to `pg_extension` on `deptype = 'e'` |

`context.configure` also enables the `rls` autogenerate plugin, which turns policy drift into a
typed `AlterRLSOp`, and sets `process_revision_directives=omit_runtime_table_info` so runtime-only
table info never leaks into a generated script.

`tests/store/test_migrations.py` proves the PostgreSQL path end to end. CockroachDB migration
coverage lives with its backend smoke and deployment checks. Both paths must reach their own head
before request traffic starts.

## Adding a migration

```bash
uv run --no-sync aizk admin database make-migration "add the thing"
uv run --no-sync aizk admin database migrate
uv run --no-sync aizk admin database check-rls
```

:::caution[Autogenerate is a drafter, not an author]
Change the model first, select the intended backend history, then read the generated script before
applying it. Autogenerate does not understand every extension or policy exclusion, so custom DDL
often needs a hand-written migration line.
:::

`uv run --no-sync aizk admin database migrate --sql` writes the offline script instead of applying it, the
fastest way to see what a revision will do.

## Next

<div class="not-content">

- [Row level security](/docs/dev/store/rls/) explains the policies this revision freezes.
- [Graph tables](/docs/dev/store/graph-tables/) covers the ontology catalogs the seed fills.
- [Upgrades](/docs/dev/run/upgrades/) has the operational side of applying a revision.
- [Development setup](/docs/dev/contributing/setup/) gets a local database running first.

</div>
