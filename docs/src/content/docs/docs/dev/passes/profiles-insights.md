---
title: "Profiles, insights, decay"
description: "The passes that summarize an entity, reflect on it, and let it fade."
---

Three passes work on the graph after it exists. One keeps a readable summary per entity, one reflects
over the graph as a whole, and one lets unused claims fade out of default recall. This page assumes
you know how a [scheduled job](/docs/dev/passes/jobs/) reaches one scope set and what the live view of
a claim means, from [The bi-temporal model](/docs/dev/store/bitemporal/). In the web app an entity is
a Subject and a fact is a Finding.

## Profiles

A profile is one evidence-grounded paragraph about one entity inside one exact scope set. The
`profile` table is unique on `(scopes, subject_id)`, so the same person can have a different profile
in your private scope and in an organization, each built only from what that scope sees.

`ProfileBuilder` in `src/aizk/graph/profiles.py` runs three phases with short transactions between
them. The snapshot loads each claimed entity's name and current fact statements through
`Fact.Live.touching(entities, settings.profile_facts_k)`, capped at 40 per entity, and an entity with
no live facts gets none. Summarizing runs eight at a time under `profile_build_concurrency` with
the `profile_system` prompt, every summary embedded in one batch. Storage is a bulk upsert on
conflict, so a rebuild never leaves a gap.

Three entry points share that builder. `build_profile()` does a single entity and raises
`NotVisibleError` when it has no visible facts. `refresh_profiles()` rebuilds every profile in the
scope, which `ProfileRefreshJob` runs weekly. `refresh_dirty_profiles()` is the incremental path and
the interesting one.

## The dirty queue

Nothing rebuilds a profile on a timer. A counter that extraction bumps drives the rebuilds.

```text
ChunkProjectionJob finishes a chunk
   -> Watermark.bump_many (kind entity_dirty, one ref per touched entity)
        -> watermark rows, one counter per entity
             -> pending_refs, the oldest 64 with counter > 0
                  -> snapshot, summarize, upsert those profiles
                       -> Watermark.consume subtracts only the observed snapshot
                          (a bump that arrived meanwhile keeps the entity dirty)
```

`ChunkProjectionJob.handle` bumps `Watermark.Kind.entity_dirty` once per entity the chunk touched, so
the watermark table holds a per-entity backlog counter. `ProfileProjectionJob` fires every minute,
reads the oldest `profile_batch_size` refs with a positive counter, 64, rebuilds those profiles, and
consumes. Consuming is a subtraction, not a reset. `Watermark.consume` lowers each counter by the
value it read at the batch start, floored at zero, so a bump that arrived meanwhile keeps the entity
dirty for the next tick. Retained failures go back with `retry_failed_profile_projections()`.

## Insights

:::note[Where this comes from]
Reflective observations are adapted from [A-MEM](https://arxiv.org/abs/2502.12110). See the full
[lineage](/docs/dev/prior-art/references/).
:::

`InsightJob` runs weekly and derives observations about the graph rather than one entity.
`InsightBuilder` in `src/aizk/graph/insight.py` grounds on the newest `insight_facts_k` live
statements, 40, excluding facts already predicated `observes` so it cannot feed on its own output.
Fewer than two and it logs a skip and writes nothing.

The model returns observations each with a statement and a significance from zero to one.
`kept_observations()` drops anything under `insight_min_significance`, 0.6, sorts the rest, and keeps
at most `insight_max`, 5. A run that clears the gate with nothing writes nothing.

What survives is written back as ordinary graph material. One entity named `graph observations` exists
per scope set, and each observation becomes a fact with that node as subject, no object, predicate
`observes` and its significance on the `attributes`. Because the fact ID derives from its own text,
`observation_already_claimed()` checks past the live gate and skips a statement this scope ever
claimed, so a repeat is not re-asserted and an archived one not resurrected. Writing insights as facts
means the fact lane retrieves them with no special case.

## Decay

:::note[Where this comes from]
Forgetting-aware scoring follows [Memora](https://arxiv.org/abs/2604.20006). See the full
[lineage](/docs/dev/prior-art/references/).
:::

`DecayJob` runs daily and calls `Fact.Claim.archive_stale`, which decides everything in one `UPDATE`.
Relevance uses the database's own clock, so no timestamp crosses into Python.

```text
  age       = now() - coalesce(last_accessed, lower(recorded))
  relevance = 0.5 ^ (age / half_life) * (1 + access_count)

  half_life = AIZK_DECAY_HALF_LIFE_DAYS,  default 90 days
  floor     = AIZK_DECAY_FLOOR,           default 0.25
```

A claim is archived when its relevance falls under the floor. With the defaults a claim nobody has
read is archived after two half lives, so 180 days, and a single read both resets the clock and
doubles the multiplier, buying another 90 days. Reads keep memory alive, on the counters recall bumps.

Archiving is not deleting. The update closes the claim's `recorded` range at `now()` and stamps
`attributes` with a `decayed` timestamp. The row stays put, leaving the live view and default recall
while staying readable to a history query or a past-date question. Only live claims with a valid
period are eligible, so one already closed by a correction is untouched.

## Dedupe and repair

The two module names are reversed from the obvious. `src/aizk/graph/dedupe.py` holds the idempotent
`claim_entity` and `claim_fact` helpers the other passes use, and the nightly merge lives in
`src/aizk/graph/repair.py` as `dedup_entities()`, which `DedupJob` runs.

The merge groups visible entity content by type and normalized name, skipping RAPTOR summaries, sorts
by ID bytes so a duplicate set elects the same winner, and builds a redirect map from each loser to
the keeper. An entity whose name normalizes to nothing redirects to null, meaning drop.

Applying it is delicate, because a fact's content ID derives from its subject, predicate, object and
statement. For each affected fact the pass snapshots the claim history, deletes the content row,
reinserts it under the same ID with corrected endpoints, and replays the claims, so no history is
lost. A fact that lost an endpoint to a null redirect is dropped instead. The duplicate's own claims
are then repointed at the keeper, except where the keeper already holds a claim in the same scope set,
where the redundant one is deleted first, and only then does the duplicate content row go away. All of
this runs under the database owner, since a merge spans what any one caller can see.

## Next

<div class="not-content">

- [Communities and RAPTOR](/docs/dev/passes/communities-raptor/) covers the two clustering passes.
- [The bi-temporal model](/docs/dev/store/bitemporal/) explains why archiving closes a range instead of deleting.
- [The lanes](/docs/dev/read/lanes/) shows where profiles and observations enter recall.
- [The job system](/docs/dev/passes/jobs/) has the schedules that trigger all of this.

</div>
