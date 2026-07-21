---
title: "Communities and RAPTOR"
description: "Detecting clusters and rolling them into a bounded summary tree."
---

These two passes build the coarse end of retrieval. Recall can already find individual facts, so what
is missing is the answer to a broad question that no single fact carries. This page assumes you know
how a [scheduled job](/docs/dev/passes/jobs/) fans out per scope set and what the `community` and
`entity` tables look like, from [Graph tables](/docs/dev/store/graph-tables/). The web app calls a
community a Theme.

:::note[Where this comes from]
Community summaries are adapted from [GraphRAG](https://arxiv.org/abs/2404.16130), and the recursive
summary tree is adopted from [RAPTOR](https://arxiv.org/abs/2401.18059). The full lineage is in
[References and lineage](/docs/dev/prior-art/references/).
:::

## The growth gate

Both passes are expensive and pointless when nothing changed, so both sit behind `run_if_grown()` in
`src/aizk/background/jobs/maintenance.py`. It counts every fact claim ever recorded in the exact scope
set, live gate skipped so closed and archived claims still count and the number only rises, and
compares that to a stored watermark. Below the threshold it logs a skip and returns without touching a
model, otherwise it builds and writes the new count back.

The two passes keep separate watermark kinds so they never consume each other's growth.
`Watermark.Kind` has four values, `fact_count` for communities, `raptor_fact_count` for RAPTOR,
`entity_dirty` for profiles and `config` for settings. Both thresholds default to 50 new facts,
through `communities_every_n_facts` and `raptor_every_n_facts`.

## Detecting communities

`build_communities()` in `src/aizk/graph/communities.py` runs in three phases with short transactions
between them. The snapshot reads every live fact in the scope that has an embedding, projected to
`subject_id`, `object_id` and `statement`, plus the names those facts touch.

`detect()` builds an undirected `networkx` graph whose edges are the facts that have an object, so a
unary fact contributes nothing to the topology. Partitioning is Louvain through
`networkx.algorithms.community.louvain.louvain_communities`, seeded with `settings.louvain_seed`,
which is 7, so a rebuild over an unchanged graph gives the same partition. The backend comes from
`settings.community_backend`, and at its `"networkx"` default the keyword is omitted, since the
in-process default and an accelerator like nx-cugraph dispatch differently. Clusters smaller than
`community_min_size`, which is 3, are dropped.

Each surviving cluster becomes a prompt holding up to `community_entities_k` sorted member names and
`community_facts_k` internal statements, both 64. A statement is internal only when its subject is in
the cluster and its object is absent or also in the cluster, so a summary is never grounded in an edge
leaving the group. The model returns a `CommunitySummary` of label and summary, four clusters at a
time under `community_build_concurrency`, and every summary is embedded in one batch.

Storage is a generation swap. Inside one transaction the pass deletes every `community` row whose
`scopes` equals this exact array and inserts the new ones, each with `label`, `summary`, `embedding`
and the cluster's `member_ids`. Communities are a projection, so throwing the old generation away
costs compute and never knowledge.

## RAPTOR over the communities

`build_raptor()` in `src/aizk/graph/raptor.py` treats those community summaries as tree leaves and
recursively summarizes upward until few enough roots remain. Fewer than two communities means nothing
to roll up and the builder returns.

```text
live facts with an embedding
   -> Louvain (seed 7, min size 3)
   -> community rows: label + summary + embedding
   -> level 0 summary entities, one per community
        |
        v
  +->  similarity graph in PostgreSQL (cosine >= 0.5)
  |      -> greedy modularity groups, split by branch factor 12
  |      -> one rollup summary per group of 2 or more
  |           dedupe: cosine >= 0.95 with a staged parent?
  |             yes -> reuse it, just add the part_of edges
  |             no  -> stage a new level N summary entity
  |      -> nodes > 3 and level <= 5 ?
  |             yes --------------------------------------+  (climb one level)
  |             no  -> atomic generation replacement
  +------------------------------------------------------+
```

`leaves()` stages one entity per community, typed `RAPTOR_SUMMARY`, with a deterministic ID from the
community label and reusing its embedding. Its claim carries `level` 0, the summary text, and the
source community ID in `attributes`.

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

- [Profiles, insights, decay](/docs/dev/passes/profiles-insights/) covers the per-entity summaries and aging.
- [The lanes](/docs/dev/read/lanes/) shows how recall actually reads communities and summaries.
- [Graph tables](/docs/dev/store/graph-tables/) has the column-level detail for both.
- [The job system](/docs/dev/passes/jobs/) has the schedules and the fan-out that trigger these.

</div>
