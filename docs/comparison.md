# Comparison

## Against grep and qmd over the vault

The vault's own tools were timed on the same queries as the engine, on the same machine.

| Scenario | vault, rg or qmd | aizk | Who wins |
|---|---|---|---|
| exact-term lookup | under 10 ms | ~475 ms | vault, grep is unbeatable on literal strings |
| lexical search, on-topic phrasing | ~190 ms | ~475 ms | tie on speed, qmd returns raw notes while aizk returns fused facts with sources |
| paraphrase of intent | no results | ~475 ms, correct doc top-ranked | aizk, BM25 misses reworded intent entirely |
| cross-document synthesis | manual, minutes | ~475 ms | aizk, graph facts and neighbors fuse sources |
| weekly review | 16 ms | 887 ms, see the benchmark caveat | tie, both instant on a settled corpus and aizk's answer is scoped and structured |
| multi-user, permissions, anonymous sharing | none | native | aizk, the vault has no concept of it |
| point-in-time replay | git archaeology | 30 ms range query | aizk |

The honest summary is a division of labor. Keep grep for literal strings, and everything that
involves meaning, time, or more than one person is the engine's ground.

## Against the engines the papers came from

| Capability | Zep / Graphiti | Mem0 | GraphRAG | aizk |
|---|---|---|---|---|
| bi-temporal facts | 4 flat timestamps | – | – | `tstzrange` pairs, Allen algebra, GiST-indexed replay |
| extraction calls per chunk | 1 combined | per memory | 1 plus gleanings | 1 plus at most one batched borderline call, rules do the rest |
| consolidation | LLM over a shortlist | LLM ADD/UPDATE/NOOP | – | uuid5, then a cosine rule, then LLM only for the 0.75 to 0.9 band |
| multi-tenant isolation | app level | app level | – | forced Postgres RLS per row, proven against the catalog |
| intersection scopes and lens | – | – | – | the scope-set lattice, implicit subset graphs |
| curation and governed publish | – | – | – | review-then-publish, an autonomous reviewer, a promoted-provenance bonus |
| runs fully local | cloud service | optional | batch pipeline | one Postgres and three vLLM containers, nothing leaves |
| retrieval fusion | graph plus text | vector | global summaries | dense, BM25, facts, pagerank, communities, RAPTOR, and profiles in one call |

Head-to-head scored runs against the live engines remain on the roadmap. This table compares
published mechanisms. The EverMemBench and TEMPO loaders ship behind
`AIZK_BENCHMARKS_ENABLED`.

The full map from each mechanism back to its paper lives in [Provenance](provenance.md).
