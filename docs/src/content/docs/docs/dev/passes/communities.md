---
title: "Communities"
description: "Cutting the entity graph into themes small enough to summarize."
---

Find finds individual facts, so what is missing is the answer to a broad question no single fact
carries. This pass clusters the entity graph and summarizes each cluster. It assumes you know how a
[scheduled job](/docs/dev/passes/jobs/) fans out per scope set and what the `community` and `entity`
tables hold, from [Graph tables](/docs/dev/store/graph-tables/). The web app calls a community a
Theme, and [RAPTOR](/docs/dev/passes/raptor/) builds the tree above this one's output.

:::note[Where this comes from]
Community summaries are adapted from [GraphRAG](https://arxiv.org/abs/2404.16130). The full lineage
is in [References and lineage](/docs/dev/prior-art/references/).
:::

## The growth gate

This pass and RAPTOR are both pointless when nothing changed, so both sit behind `run_if_grown()` in
`src/aizk/background/jobs/maintenance.py`. It counts every fact claim ever recorded in the exact
scope set, live gate skipped so closed and archived claims still count and the number only rises, and
compares that to a stored watermark. Below the threshold it logs a skip and touches no model,
otherwise it builds and writes the new count back. The two passes keep separate watermarks so neither
consumes the other's growth, both defaulting to 50 new facts.

## Detecting communities

`build_communities()` in `src/aizk/graph/communities.py` runs in three phases with short transactions
between them. The snapshot reads every live fact in the scope that has an embedding, projected to
`subject_id`, `object_id` and `statement`, plus the names those facts touch.

`CommunityDetector.detect()` builds an undirected `networkx` graph whose edges are the facts that
have an object, so a unary fact contributes nothing to the topology. Partitioning is Louvain through
`networkx.algorithms.community.louvain.louvain_communities`, seeded with `settings.louvain_seed`,
which is 7, so a rebuild over an unchanged graph gives the same partition. `community_resolution`
scales the modularity term, and the backend comes from `settings.community_backend`, where at its
`"networkx"` default the keyword is omitted since the in-process default and an accelerator like
nx-cugraph dispatch differently.

```text
live facts with an embedding
   -> one Louvain pass (seed 7, resolution 1.0)
        |
        v
  +->  cluster larger than max size 32 and shallower than depth 8 ?
  |      yes -> partition its induced subgraph again
  |               split found ------------------------------+  (refine one level)
  |               returned whole -> keep it, nothing to split
  |      no  -> keep it as a leaf
  +-------------------------------------------------------- +
   -> drop leaves under min size 3
   -> one prompt per leaf, one summary, one embedding
   -> generation swap of the community rows in this scope
```

## Why one pass is not enough

The reason is structural. Modularity carries a resolution limit, so it cannot see a community whose
internal edges number fewer than roughly the square root of the whole graph's edges. Over a
hub-shaped graph of fifty thousand entities one pass answers with a few dozen clusters of thousands
of members, and a paragraph summarizing thousands of unrelated entities says nothing.

So every cluster above `community_max_size`, 32, is partitioned again on its own induced subgraph,
where both the edge count and the limit that follows it are much smaller, and that repeats until
each leaf fits or refuses to split. A cluster Louvain returns whole, a star around a single hub for
instance, has no split left to find and survives above the bound. `community_max_depth`, 8, is only
a runaway guard, since real graphs settle after two or three rounds. Raising `community_resolution`
alone does not substitute, since it shifts the limit rather than escaping it.

The refinement is a flat partition on purpose, since RAPTOR already builds the levels above it.

`community_min_size`, 3, then drops the smallest leaves, and that filter changed meaning with the
refinement. It runs over the refined leaves now, so an entity in a two-member tail peeled off a large
cluster falls out of the theme list where before it rode along inside the blob. Measured across six
synthetic topologies of up to fifty thousand entities, that costs nothing on four of them and at most
3.8 percent of entities on the sparse, tree-like ones. Lower the setting to 2 to keep those tails, and
read the count of entities in no theme that every build logs.

## Summarizing and storing

Each surviving cluster becomes a prompt of up to `community_entities_k` sorted member names and
`community_facts_k` internal statements, both 64. A statement is internal only when its subject is in
the cluster and its object is absent or also in it, so a summary is never grounded in an edge leaving
the group. Facts are indexed by subject once per build, which keeps prompt assembly linear now that a
refined partition makes thousands of clusters. The model returns a `CommunitySummary` of label and
summary, `community_build_concurrency` at a time, and every summary is embedded in one batch.

A rebuild of a large graph is thousands of calls, so the pass refuses to treat them as one bet. A
theme whose member set has not moved since the stored generation keeps its own summary and embedding
without asking again, which is what makes a weekly rebuild proportional to what changed. A call that
fails degrades to a roster of the cluster's member names, and only a run that degrades more than
`community_summary_failure_ratio` of its themes gives up and leaves the stored generation standing.
Every build logs how many themes it summarized, carried forward and degraded.

Storage is a generation swap. Inside one transaction the pass deletes every `community` row whose
`scopes` equals this exact array and inserts the new ones, each with `label`, `summary`, `embedding`
and the cluster's `member_ids`. Communities are a projection, so throwing the old generation away
costs compute and never knowledge. [RAPTOR](/docs/dev/passes/raptor/) is queued once the swap lands
rather than on a clock of its own, since a tree built while the generation beneath it is being
replaced would describe themes that no longer exist. `admin graph communities --everywhere` runs the
same swap across every stored scope set, the catch-up path after a deploy changes how themes are cut.

## Next

<div class="not-content">

- [RAPTOR](/docs/dev/passes/raptor/) rolls these summaries into a bounded tree.
- [Profiles, insights, decay](/docs/dev/passes/profiles-insights/) covers the per-entity summaries and aging.
- [The lanes](/docs/dev/read/lanes/) shows how find actually reads communities.
- [Graph tables](/docs/dev/store/graph-tables/) has the column-level detail.

</div>
