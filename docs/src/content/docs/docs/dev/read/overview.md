---
title: "How recall runs"
description: "The seven steps between a question arriving and evidence going back."
---

One question goes in and one ranked, budget-fitted list of evidence comes back. This page walks
the seven steps in between. It assumes you know what a [scope set](/docs/dev/identity/scope-sets/)
is, since every step runs inside one, and that [the data model](/docs/dev/store/data-model/) has
already told you what a claim is.

The entry point is `recall()` in `src/aizk/retrieval/recall/orchestrator.py`, and its docstring
carries the shape.

```text
  embed | entities
        |
  recall statement, all lanes
        |
  direct-source authority and cross-encoder rerank
        |
  Python budget walk
        |
  record fact access
```

Everything below happens in `_execute`, which both `recall()` and `trace()` call.

## 1. Resolve the plan

`_execute` opens with `plan if plan is not None else Plan.maximal()`. Nothing in production passes
a plan, so production always runs the maximal one. The narrower presets on `Plan` exist only so
the eval plan study can force a comparison arm. [The lanes](/docs/dev/read/lanes/) explains what
the plan actually turns into.

## 2. Rewrite the query when the caller has a label

When `user.label` is set, the search text becomes `_speaker_query_template`, which is
`"{query}\nThe asking speaker is {label}."`. That rewritten string is what gets embedded and it is
also what binds to `qtext`, so it reaches BM25 and exact title matching too. Entity seeding
deliberately reads the raw `query` instead, because the added sentence would otherwise seed the
graph walk with the asker rather than the subject.

## 3. Embed and seed entities at the same time

`asyncio.gather` runs `EmbedClient.embed([search_query], mode="query")` beside
`query_entities(query, user)`. They hit two different sidecars and neither needs the other, so
recall pays one round trip rather than two.

`query_entities` returns an empty list immediately when `AIZK_GRAPH_ENTITY_SEEDING` is off, which
also skips the gate call entirely. Otherwise it makes sure the ontology is loaded with
`Ontology.ensure` and asks `GateClient.named_entities` for the lowered entity names the query
mentions. Those names become the `qentities` bind that PageRank seeding reads.

## 4. Build the statement, once per shape

The query context is tiny on purpose.

```python
context = QueryContext(dimensions=len(vector), fuzzy=settings.graph_mention_fuzzy)
```

Two fields, and both of them change the SQL tree rather than a value inside it. The vector width
types the `qvec` bind and `fuzzy` decides whether the trigram mention branch is compiled at all.
That is why `QueryContext` is half of the statement cache key, with `Plan` as the other half.
`build_recall_statement` is wrapped in `functools.cache`, so one `Select` object is built per
distinct context and plan and every later recall of the same shape reuses it. Construction costs
tens of milliseconds, which is worth avoiding on every call.

Every tunable value stays a named bind rather than being baked into the tree, so changing a
setting takes effect on the next call with no cache invalidation.

## 5. Execute it as the caller

```python
rows = await user.exec[Candidate](statement, qvec=vector, qtext=search_query,
                                  qentities=named, k=k)
```

`RowStatement.__call__` in `src/aizk/store/identity/user.py` merges
`settings.for_statement(statement)` underneath the explicit binds. The statement itself names the
settings fields it needs, so `rrf_k`, `recall_max_distance`, `graph_ppr_damping` and the rest
travel automatically while the explicit four win on conflict. The whole thing runs as one caller
transaction with row security on, and the rows validate into `Candidate` objects.

## 6. Order by merit

`merit_order(rows, query)` scores the first `rerank_depth` candidates with the cross encoder and
reorders them, putting maximally named sources first.
[Fusion and reranking](/docs/dev/read/ranking/) owns that story.

## 7. Pack, then record access

`pack(ranking.candidates, token_budget)` keeps the longest prefix that fits, and
[budget packing](/docs/dev/read/packing/) covers the walk and the rendering after it.

The last step is a small second transaction. `Fact.Claim.record_access` stamps `last_accessed` and
increments `access_count` for every kept candidate carrying a `fact_id`. This closes a loop,
because the fact ranking blends a recency half life over `last_accessed` with an
`ln(1 + access_count)` frequency term, so a fact only stays warm while it keeps being surfaced.

`trace()` runs this exact path with `record_access=False` and returns a `RecallTrace` instead of
candidates. That is what makes `chefe run aizk-eval trace "some question"` safe to run repeatedly.
Reading a diagnostic must not warm the memory it is diagnosing.

## Why there is no query-time router

An earlier design classified each question into a route and ran a narrower plan for it. It was
removed for three reasons that reinforce each other.

A misrouted query loses community and RAPTOR evidence outright, and no reranker can recover
evidence the SQL never returned. Overview-first packing buries fact evidence under summaries. And
the zero-shot router itself measured 44 percent accuracy on the eval strata, a figure carried over
from the plan study and repeated in the `recall()` docstring, so read it as the reason the router
was retired rather than as a fresh benchmark.

Running every lane and letting the cross encoder sort them out costs one wider statement and
removes a whole class of failure. The plan is a constant.

:::note[Where this comes from]
Keeping every lane available and ordering the result by one cross-encoder merit pass is original
to aizk rather than adopted from a paper. The [references map](/docs/dev/prior-art/references/)
marks which mechanisms are borrowed and which are ours.
:::

## Next

<div class="not-content">

- [The lanes](/docs/dev/read/lanes/) has every lane and the SQL it emits.
- [Fusion and reranking](/docs/dev/read/ranking/) covers step six in full.
- [Budget packing](/docs/dev/read/packing/) covers step seven and the rendered response.
- [Retrieval tuning](/docs/dev/read/tuning/) lists every setting the statement binds.

</div>
