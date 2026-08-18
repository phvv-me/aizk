---
title: "RAPTOR"
description: "Rolling community summaries into a bounded recursive tree."
---

`build_raptor()` in `src/aizk/graph/raptor.py` treats the [community](/docs/dev/passes/communities/)
summaries as tree leaves and recursively summarizes upward until few enough roots remain, so a broad
question can land on a summary that no single community carries. Fewer than two communities means
nothing to roll up and the builder returns.

The community pass queues this one the moment it finishes replacing a generation, so `raptor_enabled`
is off by default and the tree is never built over themes that are being swapped underneath it. It
still keeps its own `raptor_fact_count` watermark, separate from the community watermark so neither
pass consumes the other's growth, and `Watermark.Kind` holds both beside `entity_dirty` for profiles
and `config` for settings.

:::note[Where this comes from]
The recursive summary tree is adopted from [RAPTOR](https://arxiv.org/abs/2401.18059). The full
lineage is in [References and lineage](/docs/dev/prior-art/references/).
:::

## The climb

```text
community rows: label + summary + embedding
   -> level 0 summary entities, one per community, biggest 512 first
        |
        v
  +->  similarity graph in PostgreSQL (cosine >= 0.5)
  |      -> greedy modularity groups, split by branch factor 12
  |      -> one rollup summary per group of 2 or more
  |           dedupe, cosine >= 0.95 with a staged parent ?
  |             yes -> reuse it, just add the part_of edges
  |             no  -> stage a new level N summary entity
  |      -> nodes > 3 and level <= 5 ?
  |             yes --------------------------------------+  (climb one level)
  |             no  -> atomic generation replacement
  +------------------------------------------------------+
```

Leaves are the biggest communities first, capped at `raptor_leaf_limit`, 512. Level one compares every
pair of leaves in one SQL distance join, which is quadratic, so a refined partition of several thousand
communities would otherwise stall the build on tens of millions of vector comparisons. A refined
partition makes most themes a similar size, so which 512 survive the cut is close to arbitrary among
them. Treat the bound as a cost ceiling rather than a claim about importance, and raise
`raptor_leaf_limit` when a deployment wants the tree to cover more of its themes.

`leaves()` stages one entity per community, typed `RAPTOR_SUMMARY`, with a deterministic ID from the
community label and reusing its embedding. Its claim carries `level` 0, the summary text, and the
source community ID in `attributes`.

## One level at a time

Each level does three things. `similarity_groups()` sends the node embeddings to PostgreSQL and asks
for every pair whose cosine distance is at or under `1.0 - raptor_sim_threshold`, so with the default
0.5 a pair joins at cosine similarity 0.5 or better. That graph is partitioned with greedy modularity
rather than Louvain, isolated nodes surviving as singletons. Each group is chopped into runs of at
most `raptor_branch_factor`, which is 12, so no parent summarizes an unbounded fan-in, and if that
produced at least as many groups as nodes the level made no progress and the loop breaks.

`parent()` summarizes one group with `raptor_rollup_system`, feeding each child's label and the first
`raptor_child_summary_chars` characters of its summary, 384 by default. Before staging it checks
`redundant_parent()`, which reuses an already-staged parent from this level whose summary embedding is
within `raptor_redundancy_threshold`, 0.95, of the new one, so a level does not fill with
near-identical rollups. A group of exactly one member skips the model and its node rises unchanged.

`connect()` stages the structure itself. For every child a `part_of` fact is minted with the statement
`is part of <parent label>` and claimed in the same scope set, so the tree is ordinary graph material
the fact lane can already retrieve. The loop stops once the node count reaches `raptor_root_max`, 3,
or `raptor_max_levels`, 5.

## Replacing a generation

`RaptorBuilder.replace()` writes the whole plan or none of it. It takes a transaction-scoped advisory
lock keyed by the canonical scope list, so two concurrent builds of the same scope serialize, and
reselects the stale generation under it so a racer cannot resurrect or double-delete rows. It
deletes the stale `part_of` claims first, then the stale summary entity claims, mints the new contents
and claims, and only then deletes content rows no claim points at.

That last ordering matters. Content is global and scope-free while claims are scoped, so a summary
entity another scope set still claims survives the delete and only this scope's assertion of it goes
away.

## Next

<div class="not-content">

- [Communities](/docs/dev/passes/communities/) is the pass that produces these leaves.
- [Profiles, insights, decay](/docs/dev/passes/profiles-insights/) covers the per-entity summaries and aging.
- [The lanes](/docs/dev/read/lanes/) shows how find actually reads the summary tree.
- [The job system](/docs/dev/passes/jobs/) has the schedules and the fan-out that trigger these.

</div>
