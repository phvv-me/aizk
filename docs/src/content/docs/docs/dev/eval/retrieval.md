---
title: "Retrieval results"
description: "What the production retrieval bench measures and what it has measured."
---

This is a level two page in the sense of [how we evaluate](/docs/dev/eval/approach/). Everything
here measures one deployment's own memory, so read it as a regression instrument, not a score you
can hold up against another system. The commands live on [the eval CLI](/docs/dev/eval/cli/).

## What the bench measures

`chefe run aizk-eval bench` never invents a question. It reads what the corpus already holds, asks
the LLM to turn each sample into a probe, and scores the one plan production recall always uses.

```text
  live facts ─────────▶ local probe ────┐
  summaries ──────────▶ global probe ───┤
  two-hop fact pairs ─▶ multihop probe ─┤
                                        ▼
                        recall with Plan.maximal
                                        │
              ┌─────────────────────────┼──────────────────────────┐
              ▼                          ▼                          ▼
   Success@k, nDCG@k, RR        answerability judge         per-arm p50 latency
   through ir_measures               (optional)
```

Relevance is decided by the source text the probe came from. `question_scores` walks the ranked
candidates, marks the first one that equals or contains each expected statement as relevant, and
hands the qrels and the run to `ir_measures`, so hit rate, nDCG and reciprocal rank come from a
library, not from us. A multihop probe carries two expected statements and both have to match, one
candidate each.

Answerability is separate and off by default. With `AIZK_EVAL_JUDGE` set, the packed context is
rebuilt at a 1024 token budget and an LLM is asked whether it can answer the question. That is a
second opinion on the packing, not a correctness score, because the judge sees only what recall
returned.

## The three strata

**Local** probes come from visible current facts, one question per statement, keeping the proper
nouns so the subject stays identifiable. **Global** probes come from community summaries first and
RAPTOR summaries second, and the prompt forbids reusing the summary's own distinctive words so a
surface match cannot answer them. **Multihop** probes come from pairs of facts that share an
entity, where neither fact alone is enough.

Multihop gets the guardrails, because it is the easiest stratum to cheat by accident. `graph_edges`
reads 32 times as many edges as questions requested so there is slack to find real chains,
`two_hop_paths` refuses a pair that backtracks or repeats a statement, and every generated question
must contain both the starting anchor and the bridge as a contiguous run of whole words. `mentions`
matches whole word runs rather than substrings, so a short anchor like `AI` cannot match inside
`brain`. Unanchored questions are logged and dropped, and duplicates are folded by case.

## The management contract

`chefe run aizk-eval management` asks a narrower and harder question. It discovers every visible
Area and Project from declared source documents, renders twenty templated questions for each, and
checks where that subject's own current brief ranked inside the packed context. Hit means the brief
survived anywhere in the pack. First means it ranked first. Making the current brief its own
retrieval reference exposes incidental evidence that would otherwise hide a correct answer
underneath it.

A public management fixture should contain invented Areas, Projects, statuses, and notes. The
fixture must include paused and archived subjects plus nested titles such as `Atlas Migration`
inside `Atlas Migration Weekly Plan`. That case verifies that a question about the shorter subject
does not incorrectly promote the longer brief. Giving direct identity authority only to the
maximal overlapping title resolves the ambiguity without changing the retrieval plan.

:::note
This proves source identity and packing correctness, meaning the right document reaches the top of
the pack for a question about its subject. It does not judge whether an answering model then used
every field of that brief, because no answer is generated.
:::

The repository can also build public fixtures from vendored research papers. Each paper gets a
focused question and an explicit source relevance label, so anyone can reproduce the check without
access to private memory.

## The plan ablation and the router

`chefe run aizk-eval plans` keeps production honest about the plan it chose. It scores `maximal`
beside `maximal_without_raptor`, `maximal_without_communities`, `maximal_without_profiles` and
`focused` over the same questions, so removing one lane is a paired comparison rather than a rerun.
The same command sweeps the graph seeding arms, which turn `graph_entity_seeding` off, force exact
mention matching, allow fuzzy matching, and vary the GLiNER confidence floor.

Routing is measured here and nowhere else, because production stopped routing. `Route.classify`
sends the question to GLiNER2's zero-shot text-classification head, and the arm compares the
predicted route against the stratum label with a full confusion matrix. That classifier measured
44 percent accuracy on the eval strata, which is why every production recall now runs the maximal
plan and why the `Route` enum survives only as an instrument for what query-time routing would have
chosen and cost.

## Publishable results

Private corpora remain useful for local regression checks, but their questions, titles, identifiers,
and measurements do not belong in public documentation. A publishable result must use committed
synthetic fixtures or public sources and include the corpus generator, relevance labels, model
configuration, database configuration, and raw report. Absolute cosine values are model-specific,
so comparisons should use ranking metrics and margins rather than treating one distance threshold as
portable.

## Next

<div class="not-content">

- [Retrieval tuning](/docs/dev/read/tuning/) is every setting these cells moved.
- [Fusion and reranking](/docs/dev/read/ranking/) explains the lane the reranker cell scored.
- [Extraction and models](/docs/dev/eval/extraction/) covers the write side of the same posture.
- [External benchmarks](/docs/dev/eval/external/) is the only level that compares systems.

</div>
