# Benchmarks and evaluation

Every number here was measured on the live stack, one RTX 4090 shared by the embed, rerank,
and extract containers next to one Postgres, against the real vault corpus of 1,109 documents
and 3,824 chunks that built a graph of 16,162 entities and 20,396 facts.

## Build throughput

| Metric | Value |
|---|---|
| Full vault build | 35.6 min |
| Amortized per chunk | 667 ms (was 4,500 ms, 6.8x) |
| LLM calls per chunk | 1.22 (was ~13) |
| Aggregate throughput | 3,794 tok/s |
| GLiNER2 gate skip rate | 2.2% on this dense corpus |

## The full-surface drive

Every one of the 36 MCP tools was driven end to end with timings. Every failure in the run
traced to the drive script's own argument guesses or a correctly enforced gate, none to the
engine.

| Tool | Time | Tool | Time |
|---|---|---|---|
| list, audit, membership verbs | 4 to 15 ms | remember, reference | 6 to 15 ms |
| tasks_status, pending | 12 to 32 ms | ingest one note | 36 ms |
| health full report | 252 ms | setup no-op path | 141 ms |
| recall warm | 464 to 491 ms | get_context | 467 ms |
| projects, 252 portraits | 711 ms | delete_group with demotion sweep | 30 to 41 ms |
| timeline, whole graph in window | 887 ms | export_scope full dump | 6.6 s |
| force_rebuild pending slice | 11.6 s | bench, n=10 eval | 23.0 s |

The timeline number deserves its caveat. It scales with entries returned, and this graph was
built yesterday, so a week window holds all twenty thousand claims. On a settled corpus a
week's delta answered in 18 ms.

## Retrieval quality

| Metric | Score |
|---|---|
| hit@8 | 0.80 |
| nDCG@8 | 0.743 |
| MRR | 0.725 |

The full config sweep held a surprise. The cheap config with rerank and pagerank both off
scored recall@8 0.90 and nDCG 0.775 on the same set, better than the full-lanes default at a
third of the latency. The n is small (ten synthesized questions), but it argues the expensive
lanes should earn their latency per query, which is exactly what the gated query routing is
for.

## Verification posture

- 577 tests across 57 files, all green, property-based first with Hypothesis. The RLS lattice
  is proven against an independent Python specification over the full user, role,
  scope-set, and lens cross-product.
- 100.00% consolidated line and branch coverage, 3,877 statements and 454 branches with zero
  missed.
- The schema regenerates from the models, the RLS drift probe comes back empty over repeated
  fresh-volume cycles, and the downgrade path is verified.
- Five real bugs were found and fixed by the benchmark drives themselves, a concurrency
  deadlock, GLiNER thread oversubscription, a batch-killing unhandled exception, a second
  truncation shape, and the greedy date parser.
