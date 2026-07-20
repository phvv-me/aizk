# Changelog

All notable changes to aizk are documented here.

The format follows Keep a Changelog, and releases are cut from the version in `pyproject.toml`.

## Unreleased

### Added

- A SvelteKit browser dashboard under `src/web` replaces the planned Reflex Python UI, served by
  a separate browser API service. `AizkAPI` verifies the same Logto bearer tokens as MCP and
  exposes profile, overview, recall, remember, upload, and organization management routes while
  PostgreSQL row security stays the final boundary.
- `request_upload` gives agents a file upload path that never embeds bytes in a tool call. The
  MCP tool and the API service mint single-use short-TTL capability PUT URLs into one shared
  store, only the API PUT redeems them, and the uploaded original flows through the same malware
  scan and Docling conversion intake as a preserved source URI.
- Self-describing sources can declare any database-backed ontology kind with `- Type <kind>` and
  any typed relation with `- <predicate> [<object kind>] <object name>`. Project, Area, Status,
  Paper, and future kinds now share one path. Query-relevant entity catalogs derive from declared
  sources and live fact endpoints, retain exact scope sets, and join current state relations.
- Store models expose cohesive `Entity`, `Fact`, and `Relation` namespaces. Watermark and system
  constants follow the same nested interface through `Watermark.Kind`, `System.Entity`, and
  `System.Relation`.

- Recall ranks with four new signals, each validated on a planted synthetic corpus before
  landing. Multihop questions expand through an in-statement personalized PageRank seeded by
  the entities the query names (GLiNER2 extracts the mentions, an exact lowercased name match
  seeds them, and connection scoring takes the weaker endpoint's mass), lifting planted
  chain-fact recall from 32/128 to 123/128 inside the final pack while replacing the slower
  recursive walk. Fact ordering blends access recency and frequency with cosine distance, so a
  fresh, often-recalled claim outranks its stale twin (32/32 planted pairs, from 13/32).
  Dense lanes carry a relevance floor (`recall_max_distance`) that keeps off-corpus questions
  from packing garbage, and the sources lane caps hits per document
  (`recall_per_document`) so one repetitive note cannot crowd out every other source.
- Query mentions also match entity names by trigram similarity through the fused initial schema,
  with fuzzy-matched seeds carrying mass scaled by their similarity so a misspelled or
  inflected mention still seeds its entity without outweighing an exact match. Every ranking
  constant in the recall program is now a setting: seed weights, the mass window, the
  dangling-object factor, per-lane depths, the fact-candidate factor, the token estimate,
  and the fuzzy toggle.
- The embedding default was measured, not assumed: on 1,903 real vault chunks and 1,101
  title queries, `Qwen3-VL-Embedding-2B` at the schema's 1,024 dimensions retrieves within
  one to two points of `Qwen3-Embedding-4B` (hit@5 88.0% vs 90.1%, MRR 0.794 vs 0.802),
  and native dimensions add under a point, so the multimodal default and the Matryoshka cut
  both stand. The text-only models widen the off-corpus distance gap, so a text-only
  deployment can swap the checkpoint and reembed.
- Facts are grounded to their exact source spans: the extraction schema asks each fact for
  the shortest verbatim supporting quote, and the graph writer aligns it to the chunk text
  (exact first, then case- and whitespace-insensitive with an offset map that survives
  multi-character casefolds like ß to ss) into `quote_start`/`quote_end` claim attributes.
  A quote that cannot be aligned grounds nothing rather than guessing. Measured live on
  real chunks, 13 of 20 extracted facts grounded with correct recovered spans. The idea
  came from evaluating Google LangExtract head-to-head, which lost to the house extractor
  on yield, latency, and vocabulary enforcement but demonstrated char-interval grounding
  worth stealing.
- GLiNER2 moved behind one required GPU sidecar whose routes cover classification, mentions,
  relevance, and grounded graph extraction. The server process never imports torch, and an
  unavailable model fails visibly. `AIZK_EXTRACT_BACKEND` selects the production LLM extractor or
  the experimental GLiNER graph route without changing graph-building code. The service batches
  overlapping word windows through GLiNER2's public `batch_extract` API and restores source spans.
  A controlled Crimson comparison found the large checkpoint nearly as fast as base and somewhat
  more precise, but still much weaker than the LLM on relation meaning. Large therefore serves the
  cheap gate while the LLM remains the default writer.
- Ordinary 2,048-character graph chunks now fit one LLM extraction window instead of repeating
  the ontology prompt over two half-chunks. The response bounds double to preserve the former
  two-window entity and fact capacity. The extraction benchmark also selects `llm` or `gliner`
  explicitly so throughput experiments cannot silently change the quality lane being measured.
  Its bounded concurrency, wall time, completed-cases rate, and backlog ETA now measure burst
  endpoints directly. Authenticated OpenAI-compatible services can receive redacted custom
  headers, including Modal proxy credentials, without exposing an unauthenticated endpoint.
  New local extraction uses Gemma 4 12B QAT with four scheduled sequences on the dedicated RTX
  3090. Completed chunk projections remain untouched, and duplicate queue delivery now skips a
  chunk whose projection was already committed.
- Background jobs now share a typed PgQueuer boundary for payload validation, deduplication,
  priorities, fleet-wide concurrency, and database-backed retries. Profile projection work runs
  ahead of chunk projection, scheduled passes stay below both, and exhausted failures remain held
  with their deduplication keys instead of being silently recreated.
- An optional cross-encoder rerank pass between candidate retrieval and packing: with
  `AIZK_RERANK_URL` set, recall runs the same lane program cut before packing, rescoring the
  evidence lanes through `/v1/rerank` (a vLLM `Qwen3-Reranker-4B` compose service), and a
  Python packer that exactly replays the SQL packer walks the budget. Without the endpoint,
  recall stays one statement. The client wraps query and documents in the official Qwen3
  reranker prompt scaffold (`rerank_query_template`/`rerank_document_template`), which is
  load-bearing: unscaffolded, the served classifier scores junk above answers. Measured on
  real vault queries reranking the embedder's own top 8, the 0.6B checkpoint degraded MRR
  0.90 to 0.77 even correctly scaffolded while the 4B held 0.91, so 4B is the shipped
  default and the small checkpoint is never a valid economy.

- Speaker-aware capture preserves author label, role, channel, reply, phase, topic, and source
  time through chunks, working memory, graph claims, recall hits, and context blocks.
- Epistemic kinds distinguish world state, experience, observation, opinion, preference,
  procedure, and negative results. Speaker-bound kinds consolidate per creator.
- A real GroupMemBench adapter batches and imports conversation histories into isolated shared
  scopes, recalls as each asking user, generates grounded answers, and reports each question
  family separately.
- Pydantic Evals now owns typed external benchmark cases, concurrent execution, LLM judging,
  durations, and explicit operational failures. Reports record model provenance and distinguish
  diagnostic samples from the complete reference protocol.
- `FAMAScore` implements Memora's forgetting-aware accuracy equation.
- `aizk-eval groupmem` runs the external benchmark pipeline with bounded smoke-run controls.

### Security

- Closed an RLS write-policy bypass: an empty scope set made `scopes <@ writer_groups` trivially
  true, so any authenticated caller could write into another user's private space. The empty-scope
  write branch is now guarded on ownership.
- The MCP server validates a token's `aud` against its RFC 8707 resource id, so a token the issuer
  signed for another resource in the same tenant is rejected rather than accepted.
- A malformed identity-provider groups claim is skipped rather than crashing every authenticated
  request the token makes.

### Changed

- The repository adopts one `src/` layout. The `aizk` package, the deployment files, the
  evaluation harness, the GPU sidecar services, and the web frontend now live under `src/`.
- The MCP server is an agents-only surface of five tools, `status`, `recall`, `remember`,
  `share`, and `request_upload`. Every browser concern moved to the separate API service.
- The Logto client, its models, and the write policy consolidated under `integrations/logto`
  alongside the ClamAV and Docling clients. Operator probes, provisioning, and reports split
  into an `ops` package, settings into a `config` package, and the queue boundary into
  `background`.
- Test suites consolidated to mirror the package layout, with API, artifact, integration, and
  migration suites joining one tree and the duplicated queue tests folded into `background`.
- Reusable PostgreSQL columns, native enums, JSONB and pgvector operators, typed values relations,
  template expressions, and database hashing moved to the optional `patos[sql]` package. AIZK now
  imports one `patos.sql` namespace and no longer owns a general SQL helper package. Database
  hashing uses SHA-256 through `sql.uuid8`. Document content identities are native PostgreSQL UUID
  values carrying 122 digest bits with valid RFC 9562 version and variant fields. Pydantic `UUID8`
  validates the invariant at the application boundary.
- Fact UUID5 identities now use resolved subject and object IDs rather than endpoint names. Equal
  names under distinct ontology kinds therefore remain distinct. State updates close every
  occupied live value under the same relation, while set relations such as `part_of` coexist.

- Hybrid retrieval is one maximal SQLAlchemy plan built from typed lane statements. Every query
  includes local evidence, global summaries, and graph paths so routing cannot discard evidence.
  The cross-encoder orders the combined candidates on merit, and packing takes a simple token
  budget prefix. The old query-time router remains only as an evaluation instrument. `Candidate`
  validates `fact_id` and `source_chunk_id` as
  UUID7, the row-id invariant. Content-addressed and external identities use UUID5.
- Text ingestion supports stable source URIs and batches a corpus through one embedder pipeline.
- Graph writing and graph repair now live outside the extraction pipeline. Retrieval database
  reads now live outside recall orchestration.
- PostgreSQL grants and extension setup use compiled SQLAlchemy DDL elements. Queue status and
  scale storage reads use SQLAlchemy expressions rather than query strings.
- All string enums use `StrEnum` with `auto()` when member names already are the wire values.

- The session engine is reworked into composable building blocks and renamed for the row-level
  distinction it encodes: `acting_as`/`as_system` run as the RLS-enforced app role `aizk_app`,
  while `bypass_rls` runs as the owner role `aizk_admin` (formerly `aizk`) for the few cross-tenant
  content writes the app role's policies forbid.
- Identity is derived from the token, never stored. aizk keeps no user, organization, role, or
  membership table. A scoped row's `created_by` is `uuid5(oidc_subject)` provenance and its scopes are
  `uuid5(oidc_org_id)` values, and row level security reads the caller's org standing from
  per-transaction GUCs for readable, writable, public, and focused scopes that middleware binds
  from the verified token rather than a membership join. Org membership, roles (`viewer`/`editor`/
  `admin`), and publishing live entirely in Logto, so there is no local user or group operator
  surface at all. A caller resolves its scope names out of its own token's org claim, and an
  operator names target orgs by their Logto ids.
- Ontology names are canonicalized to snake_case at write time, deduping the case and spacing
  variants a case-sensitive name key used to fork into separate rows.
- Store operations read the open session from a task-local `session()` accessor, and ids use
  uuid7.

### Removed

- The external benchmark command and its JSONL dependency. It converted isolated questions to
  retrieval gold without importing the conversations, speakers, scopes, or temporal state those
  questions depend on, so the resulting score did not measure the named benchmark. The internal
  corpus eval and scale harnesses remain while proper corpus adapters are built.
- The `user_`, `group_`, and `membership` tables and their `User`/`Group`/`Membership` models
  entirely, with the whole local identity and sharing-governance surface that hung off them: the
  `aizk user` and `aizk group` CLI verbs, group creation, membership grants, and the public-group
  toggle. Identity and org standing now come from the Logto token, and the group-delete demotion
  trigger goes with the table.
- The human approval loop in full, including its timestamp gate, group flag,
  `pending`/`approve`/`reject` MCP verbs, standing approver background pass, and the
  server-wide `is_admin` flag that existed only for its cross-tenant reach. A write is canon the
  moment it lands.

### Fixed

- Blank recall reads the caller's whole visible union while blank writes still choose the personal
  singleton scope.
- Entity profiles rank by profile-summary embedding rather than entity-name embedding.
- Context packing skips an oversized early block and continues fitting smaller evidence.
- A backdated update that finishes extraction late becomes a historical interval and cannot retire
  the newer live claim.
- Text sources with stable URIs refresh edited content while distinct equal-text messages remain
  distinct documents.

- Retrieval: gap-fill truncates to the requested `k`, rerank guards a score-count mismatch, and a
  pagerank non-convergence degrades instead of failing the whole recall.
- Extraction: consolidation checks every same-predicate claim, a non-UTF-8 file no longer aborts a
  directory ingest, and the community/RAPTOR growth watermark stays monotonic under decay.
- The GLiNER2 relevance gate is re-enabled on the classification head with a `Person` floor and
  loads offline from a persistent cache; structural kinds no longer leak into the auto-create pool.

### Migrations

- Every pre-release revision is fused into `0001_init`. A pre-release Aizk database is backed up
  and rebuilt from that baseline while the separate Logto database remains intact.

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
  promotion, dedup, decay, communities, RAPTOR, profiles, and insights.
- 36 MCP tools over FastMCP, everyday memory verbs, group governance, and root-only
  maintenance and admin, with Zitadel or local API-key identity.
- The eval harness, hit@k/nDCG@k/MRR scoring, a config sweep, and EverMemBench/TEMPO dataset
  loaders gated behind `AIZK_BENCHMARKS_ENABLED`.
- Documentation at [phvv.me/aizk](https://phvv.me/aizk), the engine explained in five parts,
  a paper-by-paper provenance map, and measured benchmarks and comparisons.
