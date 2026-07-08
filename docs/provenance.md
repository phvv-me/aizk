# Provenance

Where every piece of aizk came from. The engine is built from scratch on a single Postgres, never
forked, but almost nothing in it is invented from a blank page. Most of the design either
implements a specific paper's mechanism, adapts a mechanism as one tier among several, ports a
declarative shape from another project, or takes a schema shape as reference before rejecting the
fork it came from. A few pieces are original to aizk, the row-level-security graph chief among
them, since no open memory engine offers a multi-tenant graph with per-row isolation. This page is
the map from a feature in the running system back to the paper, codebase, or post it traces to,
and the module where it lives.

## The bi-temporal core

Facts are edges, not a flat table of triples, and every edge carries two independent time
dimensions.

| Feature | Source | Where in code | Status |
|---|---|---|---|
| Facts and entities each split into an immutable, content-addressed structure shared across every tenant plus a per-container bi-temporal claim on it (a union sharing model): two owners independently extracting the identical knowledge land one structural content row, each holding their own claim on it, rather than colliding on the same primary key or duplicating the structure per tenant | Zep/Graphiti, arXiv 2501.13956, the content/claim split an original aizk refinement fixing a cross-tenant id collision the single-table shape could not | `store/models/fact.py` (`FactContent`, `FactClaim`), `store/models/entity.py` (`EntityContent`, `EntityClaim`), `graph/dedupe.py` (`mint_content`) | shipped |
| Bi-temporal `valid`/`recorded` as `tstzrange` pairs, never delete, expire instead | Zep/Graphiti, refined with `tstzrange` pairs, an original aizk refinement over Zep's four flat timestamp columns | `store/models/fact.py` (`FactClaim.valid`, `FactClaim.recorded`, `is_current` hybrid property, `visible_at`) | shipped |
| Extracted valid-time resolution for each candidate fact | Zep/Graphiti temporal grounding | `extract/llm/triples.py` (`resolve_timestamps`), `extract/models/extraction.py` (`TimedFact`) | shipped |
| ADD / UPDATE / NOOP consolidation against similar existing facts | Mem0, arXiv 2504.19413 | `extract/models/consolidation.py` (`ConsolidationVerdict`), `extract/llm/triples.py` (`decide_consolidation`), `graph/build.py` (`GraphWriter.consolidate`) | shipped |
| Content-addressed `uuid5` ids over the normalized triple and name, so a rerun converges | original (aizk) | `graph/ids.py` (`entity_id`, `fact_id`) | shipped |
| Active-record model methods: each ORM class carries its own read and write operations (`Group.create`, `Group.pending_facts`, `User.for_subject`, `FactClaim.archive_stale`, `Watermark.bump`) rather than a separate repository or service layer, so a caller reasons about one object's own API instead of a parallel query module per table | standard active-record pattern (Rails/Django ORM lineage), applied atop SQLModel | `store/models/*.py` | shipped |

## Extraction and the graph tiers

| Feature | Source | Where in code | Status |
|---|---|---|---|
| Ontology-driven extraction, entity and relation types constrained to a closed vocabulary the wire schema's grammar-constrained decoding enforces | original (aizk), general practice grounded in the ontology-engineering literature, Gruber 1995's "Toward Principles for the Design of Ontologies" names extendibility a core design criterion and Noy and McGuinness 2001's "Ontology Development 101" treats ontology development as inherently iterative, not TRACE-KG (arXiv 2604.03496), whose own thesis argues against a predefined ontology in favor of inducing one after schema-free extraction, the opposite design | `extract/ontology/cache.py` (`OntologySnapshot`, `build_snapshot`), `extract/llm/triples.py` | shipped |
| Live, table-backed ontology catalog replacing a hardcoded enum, growing by row insert rather than schema migration, `EntityContent.type`/`FactContent.predicate` foreign-keyed against it in place of a `CHECK` constraint. Grow-only, never-delete, no manual retire step, matching the never-delete posture the bi-temporal store already holds everywhere | original (aizk), the vocabulary-as-external-data shape follows the LLM-era schema-induction argument that a large fixed schema baked into every prompt eventually costs more context than it is worth (AutoSchemaKG, arXiv 2505.23628, and "Extract, Define, Canonicalize," arXiv 2404.03868) | `store/models/tables/ontology.py` (`EntityKind`, `RelationKind`) | shipped |
| Auto-create cascade minting a new entity kind from the extractor's own free-text suggestion when nothing existing fits, rules-first (embedding similarity against known descriptions folds a near-duplicate in) before ever falling through to a fresh row, the canonicalization the schema-induction papers describe done preventively at create time rather than as a later merge pass | original (aizk), the grow-then-canonicalize shape follows TRACE-KG (arXiv 2604.03496) and AutoSchemaKG (arXiv 2505.23628), whose only operations on an induced vocabulary are create and merge, never a human retire | `graph/ontology_growth.py` (`resolve_suggested_type`, `best_matching_kind`) | shipped |
| Personalized-pagerank multi-hop retrieval lane | HippoRAG 2, arXiv 2502.14802 | `graph/algos.py` (`ppr_expand`), `retrieval/recall.py` (`Recall`) | shipped |
| Community detection and summaries, the global thematic lane | GraphRAG, arXiv 2404.16130, and LightRAG, arXiv 2410.05779 | `graph/communities.py` | shipped |
| RAPTOR recursive summary tree above the communities | RAPTOR, arXiv 2401.18059 | `graph/raptor.py` | shipped |
| Reflective insights derived from the graph and written back as observation facts | A-MEM, arXiv 2502.12110 | `graph/insight.py` | shipped |
| Rolled-up entity profiles, static identity plus dynamic state | GAM, arXiv 2604.12285, taken as one tier among several rather than GAM's full hierarchical scheme | `graph/profiles.py` | shipped |
| Working-memory session tier, promoted into the graph by age or overflow | original (aizk), the cheap front write every remember lands in before the extract pipeline runs | `graph/session_tier.py`, `store/models/session_item.py` | shipped |
| Autonomous curation review: a standing LLM reviewer judges a curated group's pending queue against its own already-approved canon, approving or rejecting each claim on its own, debounced on pending count so an unchanged queue is skipped | original (aizk), the automated half of the review-then-publish loop that otherwise waits on a human admin | `graph/curation_review.py` (`review_group`, `review_curated_groups`), `background/tasks.py` (`CurationReviewTask`) | shipped |

## Sharing, governance, and the visibility lattice

| Feature | Source | Where in code | Status |
|---|---|---|---|
| Private and shared memory tiers, asymmetric time-evolving access as a bipartite membership graph, immutable provenance | Collaborative Memory, arXiv 2505.18279 | `store/models/group.py`, `store/models/membership.py` | shipped |
| Postgres row level security plus a memberships table as the read and write policies, ported as a declarative shape (a model states its own `__rls_policies__`, a mapper-construction hook reads them into shared metadata, alembic ops apply and diff them) from DelfinaCare/rls (MIT) rather than depending on the PyPI package directly, since the upstream library never emits FORCE ROW LEVEL SECURITY on its alembic path, hardcodes its GUC prefix with no override, ships a per-policy bypass escape this schema's FORCE-everywhere moat has no use for, and pulls in a hard `starlette` dependency this codebase does not use | DelfinaCare/rls (https://github.com/DelfinaCare/rls, MIT), ported not installed; the catalog-deparse normalization in `policy.py` also adapts that project's algorithm | `store/rls/__init__.py`, `store/rls/predicates.py` (`ScopeLattice.read`, `ScopeLattice.write`), `store/rls/register.py` | shipped |
| Forced RLS on every scoped table, checked and diffed by Alembic autogenerate | original (aizk), no open memory engine offers a multi-tenant graph with row-level isolation | `store/rls/ops.py` (`compare_scoped_rls`, `apply_statements`), `store/rls/verify.py` (`verify_scoped_rls`), `store/mixins/scoped.py` (`Scoped.__init_subclass__`) | shipped |
| Reader, writer, and admin membership roles | original (aizk) | `store/models/membership.py` (`Membership.Role`) | shipped |
| Public groups readable by anyone, including anonymous callers, over the RLS union | original (aizk), the shared-brain publishing switch | `store/models/group.py` (`Group.public`, `Group.publish`), `mcp/server.py` (`publish_group`) | shipped |
| Anonymous read access with a token-bucket rate limit, write refused | original (aizk) | `mcp/middleware.py` (`AnonymousRateLimit`), `mcp/user.py` (`require_identified`) | shipped |
| The narrowing lens, projecting one scope-set's composed graph out of the visible union | original (aizk) | `store/context.py` (`acting_as`, `scopes` argument), `store/rls/predicates.py` (`LENS`) | shipped |
| Governed promotion, publishing a document and its facts into a wider scope as an audited copy | Governed Shared Memory, arXiv 2606.24535 | `graph/promote.py` | shipped |
| Review-then-publish curation, a pending fact invisible until a group admin approves it (or the autonomous reviewer does, above) | inspired by brainshared.com's "reviewed into the brain" loop | `store/models/group.py` (`Group.pending_facts`, `Group.approve_facts`, `Group.reject_facts`), `mcp/server.py` (`pending`, `approve`, `reject`), `store/models/fact.py` (`FactClaim.reviewed_at`) | shipped |
| Promoted-provenance ranking boost, letting an admin-reviewed, published copy edge out an equally-ranked unpromoted hit | inspired by brainshared.com's "the best person's AI becomes everyone's floor" | `retrieval/recall.py` (`Recall`, `settings.promoted_bonus`), `store/migrations/sql/hybrid_recall.sql.j2` (`chunk_scored`) | shipped |
| Single-Postgres schema shape | Cognee, kept only as a schema reference after the fork was rejected, see the "Cognee Is the Fork Base" Zettel | n/a, design precedent only | reference, not forked |

## Serving and the retrieval stack

| Feature | Source | Where in code | Status |
|---|---|---|---|
| vLLM OpenAI-compatible containers for embedding, reranking, and extraction, co-resident on one GPU | n/a, infrastructure choice | `docker-compose.yml` (`vllm-emb`, `vllm-rerank`, `vllm-llm`) | shipped |
| Qwen3-VL-Embedding text and image lanes over `/v1/embeddings` | n/a, the served model | `serving/embed/embedder.py` (`Embedder.embed`, `Embedder.embed_images`) | shipped |
| Qwen3-Reranker cross-encoder over `/v1/rerank` | n/a, the served model | `serving/rerank/reranker.py` | shipped |
| chonkie recursive prose chunking and tree-sitter AST-aware code chunking | chonkie (library) | `serving/chunk/chonkie.py`, `serving/chunk/code.py` | shipped |
| `hybrid_recall()`, a single `language sql, stable` Postgres function fusing dense, lexical, and one-hop graph neighbor lanes in one round trip, running under the caller's own row level security since a SQL-language function carries no elevated privilege of its own | original (aizk), in-database retrieval over a hand-rolled multi-query Python fusion | `store/migrations/sql/hybrid_recall.sql.j2`, `retrieval/recall.py` (`hybrid_recall_rows`) | shipped |
| `live_fact`, a `security_invoker` view joining `fact_claim` to `fact_content` and narrowing to exactly the live version, so a caller that only wants the live graph reads one mapped class instead of re-deriving the join and the temporal predicate by hand | original (aizk) | `store/migrations/sql/live_fact.sql`, `store/models/live_fact.py` (`LiveFact`) | shipped |
| pgqueuer-backed durable queue plus cron scheduler for the background passes; procrastinate was evaluated as a replacement and rejected for two reasons, no asyncpg connector (only psycopg-family drivers, a second Postgres stack alongside the asyncpg one everything else shares) and no built-in per-user fan-out primitive (`@app.periodic` fires one bare tick, not one job per tenant, so the no-leak fan-out boundary would have to be hand-written again underneath it anyway) | pgqueuer (library), procrastinate (library, evaluated and rejected) | `background/queue.py`, `background/schedule.py` (`fan_out`), `background/tasks.py` (`ScheduledTask`) | shipped |
| SQLModel ORM, one class doubling as the mapped table and its own pydantic schema | SQLModel (library) | `store/mixins/base.py` (`TableBase`), `store/models/*.py` | shipped |
| FastMCP server with Zitadel JWT/introspection and local-key token verifiers | FastMCP (library), Zitadel (identity provider) | `mcp/server.py` (`AizkMCP`), `store/models/tables/user.py` (`User.verifier`, `User.from_token`) | shipped |
| VectorChord `halfvec` ANN index (RaBitQ/DiskANN) with an `hnsw` portable fallback | VectorChord (library) | `store/mixins/embedded.py` (`Embedded.__table_args__`), `config/settings.py` (`index_backend`) | shipped |
| VectorChord `vchord_bm25` lexical index with a native `tsvector` portable fallback | VectorChord (library) | `store/migrations/sql/hybrid_recall.sql.j2` (`lexical_chunk`), `config/settings.py` (`bm25_backend`) | shipped |
| Query routing, classifying a query as local, global, or multi-hop and narrowing the retrieval mix to that route's lanes, default off pending an eval A/B | original (aizk) | `retrieval/query_route.py` (`QueryRoute`, `RoutePlan`) | shipped, gated by `AIZK_QUERY_ROUTING` |

## Evaluation

| Feature | Source | Where in code | Status |
|---|---|---|---|
| Hit@k / nDCG@k / MRR retrieval scoring with a per-config sweep and significance test | standard IR evaluation practice | `eval/harness.py`, `eval/sweep.py` | shipped |
| EverMemBench and TEMPO dataset loaders, feeding the sweep gold from an external 2026 benchmark | the EverMemBench and TEMPO benchmark papers, the datasets themselves, not the papers' own systems | `eval/benchmarks.py` | shipped, gated by `AIZK_BENCHMARKS_ENABLED` |
| Head-to-head runs against the actual Cognee, Mem0, and Zep engines | promised in the earliest design notes | n/a | unbuilt, see `ROADMAP.md` |

## A note on two citations found in code but not in the mapping this page was built from

Two docstrings cite a source this page's ground truth list did not name, and neither citation is
vendored or otherwise verified against a paper here, so they are recorded as found rather than
folded silently into the table above.

- `config/settings.py` (`contextual_bm25`) calls its title-prefixed lexical preamble "the Anthropic
  contextual-retrieval lever," implemented in `extract/ingest.py` (`contextual_lexical`).
- `retrieval/recall.py` calls its evidence-gap re-retrieval "the IRIS-style signal" and "the
  EviMem-style re-retrieval," implemented in `Recall.fill_gap` and `has_evidence_gap`.

Both are shipped and covered by tests, the naming is just unverified against this page's source
list and worth a second pass if the provenance table is ever audited against the vendored papers
directly.
