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

## Next

Open items carried over from the earlier gap analysis, still unbuilt or partial.

- [ ] **Extract `store/rls`'s generic core as a house package.** `policy.py`, `ops.py`,
  `register.py`, and `verify.py` already import nothing aizk-specific, no `config.settings`, no
  `store.models`, only SQLAlchemy, alembic, and the generic `mixins.base.TableBase` registry
  machinery; `predicates.py` is the one aizk-aware module, hardcoding the `membership`/`group_`/
  `principal` visibility lattice. Any SQLModel project wanting declarative, alembic-diffable
  Postgres row level security could reuse the generic core as-is once it moves to `packages/`.
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
- [ ] **An as-of historical-replay index.** `store/models/fact.py` ships `ix_fact_claim_live`, a
  partial GiST index scoped to the live rows, but EXPLAIN against a seeded corpus showed the as-of
  replay path, which reads history rather than only the live graph, still falls back to a full
  GiST scan since `upper_inf` is not one of GiST range_ops's indexable operators on its own.
- [ ] **Document-level curation.** The v1 review gate, human or the autonomous standing reviewer,
  holds only facts pending (`graph/curation_review.py`), so a curated group's ingested documents
  and chunks publish immediately while their extracted facts wait for review, a gap between what
  is visible as source text and what is visible as graph knowledge.
- [ ] **Zitadel hardening end to end, plus service-account PAT docs.** The introspection and JWKS
  paths are wired (`auth/tokens.py`) and unit-tested, but not yet exercised start to finish against
  a live Zitadel instance, and there is no written guide yet for minting a service-account personal
  access token for a non-interactive caller.
- [ ] **An import counterpart to `export_scope`.** Export emits a principal-scoped, bi-temporal
  JSONL dump today (`export.py`); nothing reads one back in, so a dump is currently a one-way
  archive rather than a portable transfer.

## v1.0.0

Freeze the surface and make aizk safe to depend on.

- [ ] **Stable API.** Semantic versioning with a written migration and deprecation policy.
- [ ] **Full tested parity** on every supported platform.
- [ ] **Compatibility guarantees.** A 1.x promise for the public API.
- [ ] **Complete reference docs** in every supported language.
