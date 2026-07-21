---
title: "Graph tables"
description: "Entities, facts, communities, profiles and the catalogs behind them."
---

This is the derived half of the schema, the rows the engine works out rather than the rows a
person wrote. It assumes you know the content and claim split from
[The data model](/docs/dev/store/data-model/) and it leaves the two time ranges on `fact_claim`
to [The bi-temporal model](/docs/dev/store/bitemporal/). Modules live in
`src/aizk/store/models/tables/` and the view in `src/aizk/store/models/views/live_fact.py`.

## The shape

```d2
direction: right

kind: "entity_kind" {
  shape: sql_table
  name: "text, PK"
  description: text
  domain: "core, general, coding, research, finance, personal"
  structural: bool
  embedding: "halfvec(1024)"
}

relation: "relation_kind" {
  shape: sql_table
  name: "text, PK"
  description: text
  domain: text
  structural: bool
  policy: "set | state | event"
}

entity: "entity_content" {
  shape: sql_table
  id: "UUID5 of name and type"
  name: text
  type: "→ entity_kind.name"
  embedding: "halfvec(1024)"
}

eclaim: "entity_claim" {
  shape: sql_table
  id: "UUID7, PK"
  content_id: "→ entity_content"
  scopes: "uuid[], unique with content_id"
  attributes: jsonb
}

fact: "fact_content" {
  shape: sql_table
  id: "UUID5 of the statement"
  subject_id: "→ entity_content"
  object_id: "→ entity_content, nullable"
  predicate: "→ relation_kind.name"
  statement: text
  embedding: "halfvec(1024)"
}

fclaim: "fact_claim" {
  shape: sql_table
  id: "UUID7, PK"
  content_id: "→ fact_content"
  scopes: "uuid[]"
  valid: "tstzrange, nullable"
  recorded: tstzrange
  perspective_key: "text, default world"
  source_chunk_id: "→ chunk"
  access_count: int
}

live: "live_fact (view)" {
  shape: sql_table
  select: "claim ⋈ content WHERE is_current"
}

entity.type -> kind.name
eclaim.content_id -> entity.id
fact.predicate -> relation.name
fact.subject_id -> entity.id
fclaim.content_id -> fact.id
live.select -> fclaim.id
```

## Entities and facts

`EntityContent` is a `UUID5` over the normalized name and ontology type, so two extractors that
name the same thing land on the same row. `EntityClaim` adds the scope set and a `jsonb`
`attributes` bag, with `uq_entity_claim_content_scope` on `(content_id, scopes)` making the
claim idempotent. `EntityClaim.claim_all` inserts a whole batch with
`on_conflict_do_nothing` against that constraint.

`FactContent` is a `UUID5` over the resolved subject id, predicate, resolved object id and
statement. Using resolved endpoint ids rather than names stops two same-named entities of
different types from collapsing into one edge. `object_id` is nullable because a unary fact has
no object.

`FactClaim` is the interesting one. It carries `valid` and `recorded` as `tstzrange`,
`perspective_key` which defaults to `world` and separates an assertion about the world from one
attributed to a speaker, `source_chunk_id` back to the evidence, and the `last_accessed` and
`access_count` counters that recall updates. Its indexes are all declared on the model in
`fact.py`, including the two GiST range indexes and the partial unique index that makes a
correction append-only.

## The live_fact view

`LiveFact` is a `ViewBase` subclass, which means it is a real PostgreSQL view created
`security_invoker` rather than a CTE retyped in every statement. Its defining select joins
`fact_claim` to `fact_content` and filters on `FactClaim.is_current`, which is
`upper_inf(recorded)` and either a null `valid` or a `valid` range containing `now()`. Because
it is security invoker, the base tables' forced row security still runs as the caller.

Two small helpers in the same module supply the ranking arithmetic.

```python
def half_life_decay(age_days, half_life_days):
    return func.power(0.5, age_days / half_life_days)

def log_frequency(access_count):
    return func.ln(1 + access_count)
```

Four classmethods build the query shapes recall uses.

| Helper | What it returns |
|---|---|
| `dense` | vector seeds under the distance floor, blended with recency decay and log frequency |
| `neighbors` | one-hop graph neighbors of those seeds, each endpoint joined through its own index |
| `diffused` | seed mass spread over bounded degree-normalized hops, accumulated and cut to a window |
| `connected` | the facts that mass connects, scored by the weaker endpoint's mass |

`dense` isolates the vector index scan in a `MATERIALIZED` CTE over `fact_content` and only
then joins `live_fact` for visibility and access history. `neighbors` deliberately unions the
subject side and the object side rather than writing one `OR`, because an `OR` across both
endpoints falls back to scanning every fact.

## The catalogs

`EntityKind` and `RelationKind` both derive from `OntologyKind`, which declares
`__rls__ = rls.Open()`. That is a deliberate decision, not an oversight. The vocabulary is one
global catalog shared by every tenant, so kinds grown by a model do leak their names across
tenants. The tradeoff is recorded in the code and is meant to be revisited before multi-tenancy
hardens. `EntityKind` also carries an embedding, which `Entity.catalog` uses to pick the kinds
closest to a query.

`RelationKind.policy` is a `RelationPolicy` enum with three values.

- `set` is the default. Facts under the predicate coexist, and a near-duplicate is settled by
  distance in `Consolidator.decide`.
- `state` means one current value per subject slot. A new claim supersedes the standing one,
  and `FactWriter` collapses same-slot state candidates inside a single batch so one write
  cannot revise the same claim twice.
- `event` is declared and seeded, on `observes` and `supersedes`, but no code branches on it
  today. It behaves exactly like `set`.

## The scoped projections

| Table | Notes |
|---|---|
| `community` | `label`, `summary`, `member_ids uuid[]`, embedded. Deletable, since a pass replaces it wholesale. |
| `profile` | `subject_id` and `summary`, embedded, with `uq_profile_scope_subject` on `(scopes, subject_id)`. Mutable. |
| `session_item` | working memory with `kind`, `text`, `provenance` and `promoted_at`. `due_for_promotion` ranks aged and overflow items in one pass. |

The web app renames these for readers. Findings are facts, Subjects are entities and Themes are
communities.

## watermark and usage_event

`Watermark` is a per-scope counter and payload driving the autonomous passes, unique on
`(scopes, kind, ref)`. `Watermark.Kind` has four values, `entity_dirty`, `fact_count`,
`raptor_fact_count` and `config`. `bump` and `bump_many` increment atomically through
`on_conflict_do_update`, and `consume` subtracts a processed snapshot with `greatest(x, 0)` so
increments arriving mid-pass are never erased.

`UsageEvent` is an append-only ledger of successful operations for cost and quota accounting.
`UsageEvent.Operation` has five values, `recall`, `remember_text`, `remember_file`, `share` and
`artifact_read`. It is `Scoped` but neither mutable nor deletable, so the app role can read and
insert and nothing else. `capture_key` carries a unique index and is what makes a capture
idempotent across worker restarts, which is the entire subject of the second migration.

## Next

<div class="not-content">

- [The bi-temporal model](/docs/dev/store/bitemporal/) explains `valid`, `recorded` and corrections.
- [Row level security](/docs/dev/store/rls/) explains the content policy these claims drive.
- [The lanes](/docs/dev/read/lanes/) shows where `dense`, `neighbors` and `connected` are called.
- [Communities and RAPTOR](/docs/dev/passes/communities-raptor/) covers who writes the projections.

</div>
