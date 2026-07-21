---
title: "The data model"
description: "The content and claim union that lets one immutable statement carry many scoped assertions."
---

Everything in the store is built from one idea, so it is worth understanding before any
individual table makes sense. This page assumes you know what a
[scope set](/docs/dev/identity/scope-sets/) is and can read SQL.

## The problem

Two people in different organizations can learn the same fact. The sentence is identical, but
who may read it, when it became true, and who asserted it are all different. Storing the
sentence twice duplicates text and breaks deduplication. Storing it once with a merged
permission set leaks it.

## Content and claim

aizk splits every graph row in two.

**Content** is the immutable thing itself, the entity or the statement. Its primary key is a
UUID5 derived from its own normalized text, so the same statement always lands on the same row
no matter who writes it or how many times. Content carries no scope and no time.

**A claim** is somebody asserting that content inside a scope set, over a period. It carries the
scopes, the two time ranges, the author, the source chunk it came from, and the access counters.
Its primary key is a UUID7, so claims are ordered by creation.

```d2
direction: right

content: "fact_content" {
  shape: sql_table
  id: "UUID5, derived from the statement"
  subject_id: "→ entity_content"
  object_id: "→ entity_content, nullable"
  predicate: "→ relation_kind"
  statement: text
  embedding: "halfvec(1024)"
}

claim: "fact_claim" {
  shape: sql_table
  id: UUID7
  content_id: "→ fact_content"
  scopes: "uuid[], sorted, nonempty"
  valid: tstzrange
  recorded: tstzrange
  perspective_key: "who asserts it"
  source_chunk_id: "→ chunk"
  access_count: int
}

claim.content_id -> content.id: "many claims, one content"
```

One statement, many claims. Your organization's assertion and mine point at the same immutable
sentence, and neither can see the other's claim.

:::note[Where this comes from]
The bi-temporal entity and fact graph, statements carried with valid time and recorded time, is
adopted from [Zep and Graphiti](https://arxiv.org/abs/2501.13956). Splitting content from claim
and content-addressing the immutable half is aizk's own. The full map is at
[References and lineage](/docs/dev/prior-art/references/).
:::

## Why the split earns its complexity

It gives four things at once.

Deduplication is free, because identical text collides on the deterministic ID rather than being
compared. Corrections are append-only, because closing a claim's time range leaves the content
untouched and the history intact. Scope isolation is exact, because the scoped thing is the
claim and a reader who cannot see any claim cannot see the content either. And the embedding is
computed once per statement rather than once per organization that happens to believe it.

## Visibility follows the claims

Content tables have no `scopes` column, so their policy is defined in terms of the claims that
point at them. `ClaimedContent.__rls__` in `src/aizk/store/mixins/claimed.py` emits exactly two
policies.

```python
rls.Policy.select("content_read", content.id.in_(select(claims.content_id)))
rls.Policy.insert("content_insert", sa.true())
```

Reading a piece of content requires a visible claim on it. Inserting is always allowed, because
a content row on its own reveals nothing until a claim you can see exists, and because two
callers racing to write the same statement must both succeed.

That race is real and expected, since the ID is deterministic. `mint()` wraps the insert in a
`SAVEPOINT` and swallows only SQLSTATE `23505`, so a duplicate returns `False` instead of
poisoning the surrounding transaction. `mint_all()` tries the batch first and falls back to
row-by-row only when the batch collides.

## The mixins that build every table

Tables are assembled from mixins in `src/aizk/store/mixins/` rather than repeating columns.

| Mixin | What it adds |
|---|---|
| `Id` | `id` as UUID7, ordered by creation |
| `DeterministicId` | `id` as UUID5, derived from content |
| `Timestamped` | `created_at` and `updated_at` |
| `Scoped` | `created_by`, `scopes uuid[]`, and the row policies |
| `ClaimedContent` | the `mint` pattern and the content policies above |
| `Embedded` | `embedding` as `halfvec(1024)` plus its vector index |
| `ViewBase` | a security-invoker view rather than a table |

`Scoped` is the one that matters most. It emits `scope_read` and `scope_insert` always, plus
`scope_update` when the table is declared mutable and `scope_delete` when it is deletable, so
mutability is a property of the model rather than something a policy file has to remember.

It also supports `read_through`, which makes a child table inherit its parent's visibility. A
`chunk` is visible when its `document` is, and the child's scopes must match the parent's, so a
chunk cannot be quietly widened away from the document it belongs to.

## Two kinds of table

Some rows are things people wrote and some are things the engine worked out. The distinction is
not enforced by a column, it is a rule about who may delete what.

Sources are `document`, `chunk`, `artifact`, `artifact_content` and `blob`. They are the record.

Projections are `community`, `profile`, and the RAPTOR summary entities. They are rebuildable, a
pass may replace them wholesale, and losing them costs compute rather than knowledge. Facts sit
in between, since they are derived from sources but carry their own history and are never
deleted, only closed.

## Where to go next

<div class="not-content">

- [Content and artifact tables](/docs/dev/store/content-tables/) has the source side, column by column.
- [Graph tables](/docs/dev/store/graph-tables/) has the derived side.
- [The bi-temporal model](/docs/dev/store/bitemporal/) explains the two ranges on every claim.
- [Row level security](/docs/dev/store/rls/) explains how the policies are generated and checked.

</div>
