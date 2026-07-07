# Roadmap

Where aizk is headed. This document tracks what works today and what
each milestone needs. It is a direction, not a contract, so order and scope can shift.

## Today (0.0.x)

The core loop runs end to end, and a large rework since the initial scaffold turned it from secure
hybrid search into an actual shared-brain memory engine. See `docs/provenance.md` for exactly which
paper or design each piece traces to.

- [x] Initial public scaffolding with CI, docs, and a version-driven release pipeline.
- [x] The core loop works end to end, `ingest` or `remember` through `recall` and `get_context`,
  over the MCP tool surface (`AizkMCP` in `mcp/server.py`).
- [x] Bi-temporal store on SQLModel plus `tstzrange` valid and recorded windows, ADD/UPDATE/NOOP
  consolidation, and content-addressed ids (`store/models/fact.py`, `graph/build.py`).
- [x] The shared-brain permission lattice, reader/writer/admin roles, public groups readable
  anonymously under a rate limit, curated groups with a review-then-publish queue (including the
  autonomous standing reviewer), the narrowing lens, and governed promotion (`store/rls/`,
  `store/models/group.py`, `graph/curation_review.py`, `graph/promote.py`).
- [x] Api-only serving. Every model-shaped step, embedding, reranking, and extraction, runs in a
  co-resident vLLM OpenAI-compatible container, so there is no in-process torch or Ollama backend
  to configure (`docker-compose.yml`, `serving/embed/embedder.py`).
- [x] The background registry, one `ScheduledTask` per maintenance pass (decay, dedup, communities,
  RAPTOR, profile refresh, self-improve, session promotion, insight, curation review) fanned out
  per principal through a pgqueuer worker (`background/tasks.py`, `background/schedule.py`).
- [x] The retrieval lanes beyond plain hybrid search, personalized pagerank, community summaries,
  the RAPTOR tree, and rolled-up entity profiles, all fused into one `recall` call
  (`retrieval/recall.py`).
- [x] The as-of historical-replay path already serves off the existing schema, measured rather than
  assumed. `store/models/fact.py`'s plain, non-partial `ix_fact_claim_recorded` GiST index, not the
  partial `ix_fact_claim_live` the earlier note pinned the concern on, is what the planner picks for
  `FactClaim.visible_at(as_of)`'s `recorded @> as_of` predicate; `EXPLAIN (ANALYZE, BUFFERS)` against
  a seeded corpus (60,000 facts, 300,000 claim versions) showed a Bitmap Index Scan on
  `ix_fact_claim_recorded`, not a sequential scan, at 30 ms end to end. No composite or additional
  index is warranted at this scale.
- [x] `store/rls`'s generic core shipped as its own house package, `packages/rls`
  (https://github.com/phvv-me/rls, forked from DelfinaCare/rls, MIT), rather than staying an
  in-tree module: policy compilation, DDL assembly, `sqlglot`-based clause comparison against the
  live catalog, and Alembic autogenerate integration all now live there, generic over any
  SQLAlchemy `DeclarativeBase`/`SQLModel` registry with a configurable GUC namespace, no aizk
  import anywhere in it. `store/rls/` shrank to `predicates.py` (the aizk-specific visibility-
  lattice expressions), `register.py` (the mapper-construction hook that also tracks
  `metadata.info["rls"]`, the autogenerate guard set), and `ops.py` (the table-name-only
  `apply_scoped_rls`/`drop_scoped_rls` Alembic ops the committed `0001_init.py` migration already
  calls, kept alive as aizk's own thin wrapper over the library's DDL builders since that call
  shape predates the library's own self-contained `apply_rls`/`drop_rls`).

## Next

Open items carried over from the earlier gap analysis, still unbuilt or partial.

- [ ] **Head-to-head eval baselines.** The earliest design notes promised scoring aizk against the
  actual Cognee, Mem0, and Zep engines. The EverMemBench and TEMPO dataset loaders exist
  (`eval/benchmarks.py`) and the sweep can score aizk on them, but nothing yet runs the competing
  engines side by side on the same questions.
- [ ] **SPECTER2 or another scientific embedder as a selectable option**, the deferred lever from
  the original design, still open since the shared Qwen3-VL-Embedding model covers the general
  case well enough that a domain-specific swap has not been forced yet.
- [ ] **A finer multimodal document lane**, page, figure, and table level embedding in the shape of
  ColQwen3's late interaction, for documents whose substance is diagrams and tables rather than
  prose. Plain whole-image embedding already ships (`ingest_image`, `Embedder.embed_images`), this
  is the document-structure-aware tier above it.
- [ ] **Full batch-invariant determinism mode**, the reproducibility-2 / yamone-deterministic-
  inference tie-in flagged directly in `graph/ids.py`. Content-addressed ids and temperature-0
  extraction already make a rerun converge; this would pin the embedder and LLM kernels themselves
  bit for bit.
- [ ] **Document-level curation.** The v1 review gate, human or the autonomous standing reviewer,
  holds only facts pending (`graph/curation_review.py`), so a curated group's ingested documents
  and chunks publish immediately while their extracted facts wait for review, a gap between what
  is visible as source text and what is visible as graph knowledge.
- [ ] **Zitadel hardening end to end, plus service-account PAT docs.** The introspection and JWKS
  paths are wired (`store/models/principal.py`) and unit-tested, but not yet exercised start to
  finish against a live Zitadel instance, and there is no written guide yet for minting a
  service-account personal access token for a non-interactive caller.
- [ ] **An import counterpart to `export_scope`.** Export emits a principal-scoped, bi-temporal
  JSONL dump today (`export.py`); nothing reads one back in, so a dump is currently a one-way
  archive rather than a portable transfer.
- [ ] **Erasure, a `forget(document)` tool plus content garbage collection.** Supersession handles
  wrong knowledge but not knowledge that should never have been stored, a secret or a mistaken
  ingest, whose payload stays readable in bi-temporal history forever. Deletion is deliberately
  absent from the everyday surface (the knowledge lifecycle wants "no longer current", never
  "never happened"), so erasure arrives as one narrow admin-grade verb that removes a document,
  its chunks, and the claims derived from them, sweeping derived claims through `source_chunk_id`
  before that `SET NULL` foreign key erases the trail. A background GC pass then collects content
  rows left with zero claims, the same pass the known claim-less-orphan gap already needs.
- [ ] **Compound-engineering borrows.** Every's compound-engineering plugin
  (https://every.to/guides/compound-engineering, https://github.com/everyinc/compound-engineering-plugin)
  is, mechanically, a weaker file-based cousin of aizk's capture→facts→recall loop: it writes typed
  "Learnings" to `docs/solutions/` markdown, consolidates them with a hand-rolled ADD/UPDATE/NOOP,
  refreshes stale ones, and grounds each plan against them. aizk's Stop-hook capture already is an
  automatic `/ce-compound`, its SessionStart recall the grounding return arrow, its insight pass
  the "Pattern generalized from several Learnings", and bi-temporal supersession subsumes their
  whole `ce-compound-refresh`. Four ideas map cleanly onto existing machinery and are worth
  borrowing: a first-class **DeadEnd/NegativeResult** entity (or a `rejected_because` relation) so
  recall can warn "you tried X, it failed because Y", the most expensive and first-to-vanish
  knowledge their bug track centers; a task-scoped **`ground(task)`** MCP tool that returns exactly
  the Decisions/Patterns/Gotchas relevant to a described piece of work rather than the generic
  recent-items recall; an **incident-versus-standing altitude** on facts, the mechanism for the
  "learns standing preferences" goal where a durable preference is promoted from repeated
  incident-level observations by a recurrence trigger on the insight pass; and **confidence
  anchors** on the insight pass so an observation corroborated by N independent facts earns higher
  confidence while a single-source one stays advisory and decays, the guardrail against the
  reflective pass writing overconfident junk. Their execution harness (worktrees, PR automation,
  CI repair) stays out of scope, aizk is the memory substrate such a harness stores into, not a
  workflow orchestrator.

## v1.0.0

Freeze the surface and make aizk safe to depend on.

- [ ] **Stable API.** Semantic versioning with a written migration and deprecation policy.
- [ ] **Full tested parity** on every supported platform.
- [ ] **Compatibility guarantees.** A 1.x promise for the public API.
- [ ] **Complete reference docs** in every supported language.
