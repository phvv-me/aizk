---
title: "Content and artifact tables"
description: "Documents, chunks, artifacts and blobs, column by column."
---

These five tables hold the record, the rows a person or an agent actually wrote. This page
assumes you have read [The data model](/docs/dev/store/data-model/) and know what the `Scoped`
mixin adds to a table. Everything described here lives in `src/aizk/store/models/tables/`, one
module per table.

## The shape

```d2
direction: right

document: "document" {
  shape: sql_table
  id: "UUID7, PK"
  scopes: "uuid[], sorted, nonempty"
  title: "text, nullable"
  subject_type: "→ entity_kind.name"
  source_uri: "text, nullable"
  content_hash: "UUID8 of the source"
  artifact_id: "→ artifact"
  artifact_content_id: "pair half"
  expires_at: timestamptz
}

chunk: "chunk" {
  shape: sql_table
  id: "UUID7, PK"
  document_id: "→ document, CASCADE"
  ord: int
  text: text
  lexical: "text, nullable"
  embedding: "halfvec(1024)"
  processed_at: timestamptz
}

artifact: "artifact" {
  shape: sql_table
  id: "UUID7, PK"
  name: "text, nonempty"
  source_uri: "text, nullable"
  promoted_from: "→ artifact"
}

content: "artifact_content" {
  shape: sql_table
  id: "UUID7, PK"
  artifact_id: "→ artifact, CASCADE"
  blob_id: "→ blob, RESTRICT"
  revision: "int, > 0"
  state: artifact_content_state
  markdown: "text, nullable"
  companion_text: "text, nullable"
  docling_json: jsonb
}

blob: "blob" {
  shape: sql_table
  id: "UUID7, PK"
  content_hash: "UUID8 of the bytes"
  size: int
  stored_size: "int, <= size"
  encoding: blob_encoding
  storage_key: "text, unique"
}

chunk.document_id -> document.id
content.artifact_id -> artifact.id
content.blob_id -> blob.id
document.artifact_content_id -> content.id: "composite pair"
```

## document and chunk

A `Document` is one scoped source item. It carries `title`, `subject_type` which foreign-keys
into the `entity_kind` catalog, `source_uri`, an `observed_at` and `expires_at` pair, and
`content_hash`, a `UUID8` fingerprint of the source text. `promoted_from` points at the
document a share was copied from.

Two uniqueness rules fence duplicates. `uq_document_source_scope` is a plain unique constraint
over `(source_uri, scopes)`. `uq_document_subject_title_scope` is a **partial** unique index
over `(subject_type, title, scopes)` with `WHERE subject_type IS NOT NULL AND title IS NOT NULL`,
so untitled sources never collide with each other while a declared ontology subject can exist
only once per scope set. `Document.identifies` builds the matching lookup predicate and
`Document.identity_key` returns the batch key that mirrors it.

A `Chunk` is one ordered span of a document with its own `embedding`. It sets
`read_through = "document"`, so its row policy inherits the parent's visibility instead of
re-deriving it, and its scopes must equal the parent's. It is the only source table that is
both `mutable` and `deletable`, which means a document itself can never be deleted through the
restricted app role. `ix_chunk_pending` is a partial index on `id` with
`WHERE processed_at IS NULL`, keeping the backlog index proportional to outstanding work rather
than to the whole corpus.

### The BM25 column is not on the model

`Chunk` has no `bm25` field. The `bm25vector` column, the `chunk_bm25_sync` trigger that fills
it from `coalesce(lexical, text)`, and the `ix_chunk_bm25` index are all created by `0001_init`
through `bm25_lexical_statements()` and nowhere else. `Chunk.fused()` reaches it as a raw
`column("bm25")`, and `src/aizk/store/migrations/env.py` teaches Alembic autogenerate to ignore
both the column and the index so the gap never surfaces as drift. See
[Migrations and DDL](/docs/dev/store/migrations/).

## artifact, artifact_content and blob

An `Artifact` is the stable identity of one file inside one exact scope set. Its revisions live
in `ArtifactContent`, which sets `read_through = "artifact"` for the same reason a chunk reads
through its document.

`ArtifactContent.State` is a native PostgreSQL enum with five values, `pending`, `queued`,
`processing`, `ready` and `failed`. It records the durable business outcome and it is
deliberately not PgQueuer's delivery state, which stays the source of truth for leases and
retries. Three unique constraints hold the table together. `uq_artifact_content_revision` on
`(artifact_id, revision)` keeps revisions dense, `uq_artifact_content_blob` on
`(artifact_id, blob_id)` binds each blob to at most one revision of an artifact, and
`uq_artifact_content_artifact_id_id` on `(artifact_id, id)` exists only so something else can
point at it.

A `Blob` is object-store metadata, never bytes. It records `content_hash` as a `UUID8`, the
logical `size`, the `stored_size` after encoding, the opaque `storage_key`, and integrity
observations. `Blob.Encoding` has two values, `identity` and `zstd`. Four check constraints
guard it, including `stored_size <= size`, so a compressed object can never claim to be larger
than the original.

### Why blob is not Scoped

`Blob` has no `scopes` column, because one physical object is shared by every scope set that
was given the file. It declares its own `__rls__` in `blob.py` instead.

```python
rls.Policy.select("blob_read", cls.id.in_(select(content.c.blob_id)), roles=(settings.app_role,))
rls.Policy.insert("blob_insert", sa.true(), roles=(settings.app_role,))
```

Metadata is readable only through an `artifact_content` row the caller can already see, the
same shape as the content policy on the graph side. A blob nobody references is invisible to
everybody. Insert is open for the same reason content insert is open, since a freshly uploaded
row reveals nothing until a visible revision points at it. A `SECURITY DEFINER` guard trigger
installed by `0001_init` then rejects an insert that attaches a blob the caller could not
legitimately reach, and makes `blob_id` immutable once committed.

## The composite foreign key

`Document` links back to the exact revision it was ingested from through two columns and one
constraint.

```python
ForeignKeyConstraint(
    ("artifact_id", "artifact_content_id"),
    ("artifact_content.artifact_id", "artifact_content.id"),
    name="fk_document_artifact_content_pair",
    ondelete="SET NULL",
)
```

A single foreign key on `artifact_content_id` alone would let a forged pair name a revision
belonging to somebody else's artifact. The composite key makes PostgreSQL check the pair, so
the revision must truly belong to the artifact named beside it, and that is what
`uq_artifact_content_artifact_id_id` exists to support. `Artifact.share` relies on the same
pairing when it reads the source, and it takes a transaction-scoped advisory lock keyed by the
target's dedup identity so the target lookup, the blob dedup and `max(revision) + 1` cannot
race a peer.

## Next

<div class="not-content">

- [Graph tables](/docs/dev/store/graph-tables/) covers the derived half of the schema.
- [Row level security](/docs/dev/store/rls/) explains `read_through` and how policies are generated.
- [Migrations and DDL](/docs/dev/store/migrations/) has the BM25 statements and the blob guard trigger.
- [Artifacts](/docs/dev/write/artifacts/) follows a file through conversion.

</div>
