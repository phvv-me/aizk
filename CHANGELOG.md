# Changelog

All notable changes to aizk are documented here.

The format follows Keep a Changelog, and releases are cut from the version in `pyproject.toml`.

## Unreleased

## 0.0.1 - 2026-07-04

### Added

- The content and claim store, entities and facts split into immutable content rows plus
  per-container bi-temporal claims, so identical knowledge extracted twice never duplicates.
- The scope-set lattice, `scopes uuid[]` on every row, forced Postgres row level security
  compiled from the models, and implicit intersection scopes for groups with no standing group
  of their own.
- The write path, chunking, a GLiNER2 gate, one combined extraction call, and a rules-first
  consolidation cascade, averaging 1.22 LLM calls per chunk.
- The read path, `recall()` fusing dense, lexical, graph-neighbor, community, RAPTOR, and
  profile lanes behind one hybrid Postgres function plus a cross-encoder rerank.
- Autonomy, a pgqueuer-backed worker and cron scheduler driving graph build, session
  promotion, dedup, decay, communities, RAPTOR, profiles, insights, and curation review.
- 36 MCP tools over FastMCP, everyday memory verbs, group governance, and root-only
  maintenance and admin, with Zitadel or local API-key identity.
- The eval harness, hit@k/nDCG@k/MRR scoring, a config sweep, and EverMemBench/TEMPO dataset
  loaders gated behind `AIZK_BENCHMARKS_ENABLED`.
- Documentation at [phvv.me/aizk](https://phvv.me/aizk), the engine explained in five parts,
  a paper-by-paper provenance map, and measured benchmarks and comparisons.
