---
title: "How we evaluate"
description: "Three levels of evidence, and what each one is allowed to claim."
---

aizk keeps three kinds of evidence apart and never lets one borrow the authority of another. This
page assumes you know roughly [how recall runs](/docs/dev/read/overview/) and that you can open
`src/eval/`.

## The three levels

```text
  level 1   the test suite               proves a contract holds
            tests/, src/eval/            Hypothesis properties, backend matrices,
                                         one real migration on a throwaway database
               │
               ├─ the frozen ablation gate
               │  tests/benchmark/       proves a change did not regress a fixed
               │                         question set, under a paired statistical test
               │
  level 2   the production bench         measures the memory one deployment
            chefe run aizk-eval bench    actually holds, on one day, on one machine
               │
  level 3   an external benchmark        imports somebody else's corpus into an
            chefe run aizk-eval groupmem isolated scope and answers their questions
```

Each level answers a different question, so each one is allowed a different sentence.

## Level one, the suite

The suite proves contracts. It uses Hypothesis for the algebraic and authorization properties,
parametrized cases for backend matrices, and a migration test that creates a disposable PostgreSQL
database, upgrades it to head, writes a real scoped `document` and child `chunk`, and then checks
that the child insert policy demands both the parent document ID and the exact scope set. The RLS
verifier walks every scoped table and policy in the catalog, and Alembic autogenerate must return an
empty revision against the current models.

What that buys is certainty about behavior. What it cannot buy is any claim about answer quality,
because none of it asks a model anything. [Testing](/docs/dev/contributing/testing/) owns the suite
in detail.

## The gate between level one and level two

Sitting inside the suite is one benchmark that behaves like a measurement. `chefe run bench-aizk`
runs the frozen retrieval ablation and writes reports, and `chefe run bench-aizk-gate` runs the same
arms and compares them against the blessed baseline in
`tests/benchmark/data/retrieval_baseline.csv`. Both point at the `aizk_verify` database and both
require `--eval-mode` explicitly, so a benchmark can never run by accident inside the ordinary gate.

The comparison is per query rather than per average. Every arm, stratum and metric pair gets a
seeded paired permutation p-value, Holm correction is applied across the whole family, and a failure
needs both Holm significance and a paired Cohen's dz of at least 0.2. Run noise alone therefore
cannot fail a build. The baseline also carries `retrieval_baseline.meta.json` with the corpus
SHA-256, the judge model, `k` and the arm list, and a run whose metadata differs is rejected as a
usage error rather than silently compared.

:::note[Two honest limits]
The committed question corpus is a two question placeholder today, so the gate currently guards the
plumbing and the statistics rather than retrieval quality, and it becomes a quality gate only once a
real corpus is frozen with `chefe run aizk-eval freeze`. And a baseline is only meaningful against
the same corpus, so reblessing with `--force-regen` is a deliberate act.
:::

## Level two, the production bench

`chefe run aizk-eval bench` samples the memory a deployment already holds, turns each sample into a
probe with the LLM, and scores the maximal plan that production recall always uses. That is a real
corpus, which is exactly its strength and exactly its limit. It tells you whether this deployment
got better or worse between two commits. It says nothing you can put next to another system's
published number, because nobody else has your corpus.
[Retrieval results](/docs/dev/eval/retrieval/) holds what it has measured.

## Level three, an external benchmark

Only a level three run compares aizk with anything. It brings its own conversations, imports them
into a deterministic isolated scope in a separate database, and answers the released questions
through the same write, recall, answer and judge path a person would use. That is the only level
whose score means the same thing to a stranger, and it is also the level where aizk currently has no
published number. [External benchmarks](/docs/dev/eval/external/) explains why and what would have to
be true first.

## A number without its conditions is not evidence

This is enforced in code rather than asked for in a review. `BenchmarkReport.publishable` is computed
from five conditions at once, the complete corpus, unsampled questions, a solvability-filtered
domain, the reference protocol, and zero operational failures. Any limit flag flips the rendered
scorecard from `publishable` to `diagnostic`, and the report prints the agent model and the judge
model every time.

`ExtractionReport` refuses the same shortcut from the other direction. A model turn that fails schema
validation or times out is kept as a failed case with its error string rather than scored as a wrong
prediction, and the rendered line always carries `failed=` beside the F1. A benchmark that quietly
drops its failures reports the quality of the subset that happened to work, which is not the quality
of anything.

## Why the dated cells stay dated

The pages that follow are full of measurement cells that name a date, a corpus size, a model, a GPU
and a database version. Averaging them into one headline figure would read better, and we do not,
for a boring reason. Every one of those numbers is a function of its conditions, and the conditions
changed between the cells.

An extraction latency on a 31B checkpoint with a dedicated RTX 3090 is not comparable to the same
number on 12B. A recall p50 over a hundred thousand chunks is not the same measurement as one over
two thousand. A retrieval hit rate on the vault is not one on planted synthetic ground truth.
Averaging those would produce a number that describes no run that ever happened, and worse, would
strip exactly the metadata a reader needs to decide whether it applies to them.

So a cell is written as a pinned observation. It carries its date, its corpus, its hardware and its
model, it says what it proves and what it does not. When conditions change we add a new cell rather
than update the old one, and when a cell stops being relevant we say so instead of quietly deleting
it.

## Next

<div class="not-content">

- [The eval CLI](/docs/dev/eval/cli/) is every command and the question it answers.
- [Retrieval results](/docs/dev/eval/retrieval/) has the production bench and its dated cells.
- [Extraction and models](/docs/dev/eval/extraction/) has the graph writer and model selection.
- [External benchmarks](/docs/dev/eval/external/) has GroupMemBench and the claims we avoid.

</div>
