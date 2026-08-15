# Changelog

All notable changes to aizk are documented here.

The format follows Keep a Changelog, and releases are cut from the version in `pyproject.toml`.

## Unreleased

### Added

- An operator console at `admin.example.com`, gated by one Logto role. `aizk-admin` joins `aizk-user`
  as a second managed global role, carrying the same `control` API permission but never default,
  and `admin auth apply` now reconciles it instead of deleting it as an obsolete managed role. A
  new `admin auth roles` command prints every role under the managed prefix with the accounts
  assigned to it, and `GET /api/me` reports `admin` so the browser app can show operator pages to
  the people who hold the role. The console itself is a second Caddy site on 8082 in the existing
  container, with `oauth2-proxy` authorizing every path against that role, Grafana under
  `/grafana`, `/traces` reserved for a later tracing service, and the operator pages under
  `/app/admin`. It is one hostname carrying paths rather than a name per tool because Cloudflare's
  free certificate covers only first-level subdomains. Logto's own console moves to
  `console.example.com`, since it cannot be served under a path.

- `share` can select documents by question and can move instead of copy, so an agent no longer
  needs a document ID it had no way to obtain. Recall now prints the source document's ID and
  capture day under evidence grounded in a stored source, which makes the ordinary flow recall,
  read the IDs, share. The result names every document it carried with its title and its copy in
  the destination. `move` copies into the destination and then retires the private original, so
  ordinary recall returns only the destination copy while the original's rows, bytes, and
  `promoted_from` chain stay in place.

  Only an explicit `documents` list ever writes. A `query` answers which of the caller's own
  private documents it would select and writes nothing whatever else is passed with it, because a
  question matches on ranked similarity rather than on intent and must not be able to hand a dozen
  private notes to an organization in one call. Sharing a topic is therefore two steps, a query to
  read the candidates and a second call naming the approved IDs. `move` alongside a query is
  refused rather than quietly ignored, so a refusal can never read as a move that happened, and
  `dry_run` previews an explicit list, which is worth doing before any move.

  Both halves of a move commit in one transaction, so a failure can never leave a copy standing
  without retiring what it copied. A share whose source was revised since an earlier share
  refreshes that copy rather than reusing stale text, which is what keeps a move from retiring a
  source whose destination still holds the previous draft. A query selection and a move both
  require an organization destination and skip documents already standing there, since carrying a
  scope onto itself would add one generation per repeat. A move only ever touches the caller's own
  private documents, and repeating any of these calls is a no-op.

- Extraction can run on a hosted model instead of the local vLLM lane. Point the container's
  `AIZK_RUNTIME_LLM_URL` at an OpenAI-compatible endpoint outside the deployment and aizk
  recognizes that on its own, then applies the external posture unasked, meaning zero data
  retention, a pin to one provider so an outage on the primary never silently reroutes to an
  endpoint with a different price, quantization or retention posture, reasoning disabled because
  extraction pays nothing for hidden thinking, and a sticky `session_id` that keeps the provider
  prompt cache warm. Setting `AIZK_LLM_EXTRA_BODY` or `AIZK_LLM_HEADERS` yourself replaces that
  posture whole rather than merging into it. No cache header is sent, because OpenRouter's
  `X-OpenRouter-Cache` stores the full request and response at the edge for its TTL whatever the
  per-request retention flag says, while extraction never repeats an identical request and so buys
  nothing for the retention surface it would add.

  Name the dated slug, `deepseek/deepseek-v4-flash-0731` rather than the floating
  `deepseek/deepseek-v4-flash`. The alias has no endpoint that satisfies zero data retention, so
  under this posture OpenRouter fails closed with a 404 data policy error rather than quietly
  routing to a provider that keeps the text. Compose reads the credential from
  `AIZK_OPENROUTER_API_KEY` and passes it through as the provider-neutral `AIZK_LLM_API_KEY`, and
  the new `external_llm_carries_key` validator refuses at startup to run an external endpoint with
  an empty credential, since otherwise the anonymous request fails 401 inside a background
  extraction job far from the environment that caused it.

- `aizk admin data rechunk` re-splits and re-embeds converted originals straight from the Markdown
  already in PostgreSQL, which is the cheap catch-up after a chunk size, lexical prefix or
  embedding model change. Reconversion pays Docling and OCR to rebuild text that never changed,
  while this pass never touches the original bytes and costs one embedding batch per original. The
  work runs through the new `MarkdownReindexJob` at the conversion priority and under
  `docling_concurrency`, so a corpus-wide sweep cannot point every worker at the embedder at once.
  A finished job stamps `indexed_at` on the revision and the sweep takes the least recently indexed
  first, so repeating it with `--limit` marches through the corpus instead of circling the same
  head, and a revision converted before that column existed reads as null and is swept first.

- A nightly `CleanupJob` at 01:00 trims PgQueuer's completed-job log past
  `AIZK_CLEANUP_LOG_RETENTION_DAYS`, seven by default, in batches of
  `AIZK_CLEANUP_LOG_DELETE_BATCH`, ten thousand, looping until a short batch ends the drain, and
  then runs `VACUUM (ANALYZE)`. Nothing reads that history for correctness and it becomes the
  single largest table a busy deployment owns, so leaving it unbounded was pure weight. The vacuum
  never takes an exclusive lock and never hands disk back to the operating system either, so a
  deployment whose log grew before this job first ran wants one `VACUUM FULL pgqueuer_log` or a
  `pg_repack` pass, which the PostgreSQL page now spells out.

- Artifact bytes are compressed with Zstandard before they reach the object store and restored on
  the way back, so nothing above the byte store ever sees a compressed frame. It is adaptive rather
  than blind, keeping the compressed form only when it saves at least
  `AIZK_OBJECT_STORE_COMPRESSION_MIN_SAVINGS`, which is what stops JPEG and EPUB from paying for a
  container that buys nothing. On a copy of the live deployment, 327 objects and 459.8 MB of
  originals, the whole store came back 42.3 percent smaller, HTML 72.5 percent and PDF 46.5 percent
  where it compressed at all. The content hash still describes the original bytes rather than the
  container, so it survives recompression and an integrity pass still proves the object decodes to
  the file that was accepted. `AIZK_OBJECT_STORE_COMPRESSION_LEVEL` defaults to 9, measured as the
  knee on a 165 MB corpus of real documents, past which the write cost rises two orders of
  magnitude to buy about one more point.

  `blob.encoding_level` records the level each object was last evaluated under, and null means the
  policy is unknown, which is what every object written before that column existed reports. `aizk
  admin storage compact --limit 200` takes the largest objects still below the configured level,
  rewrites each one under a fresh key and repoints PostgreSQL before deleting the old key, so an
  interruption leaves an unreferenced object behind rather than a row pointing at bytes that are
  gone. Run it until `examined` comes back zero. Because a rewrite has to verify the original
  first, the pass doubles as an integrity check and records itself as one, and it refuses to run
  while compression is disabled, since there would be no policy to compact toward.

- Intake refuses a format aizk cannot read, at the door rather than after it is stored. A declared
  media type is a claim and the leading signature is the evidence, and a file whose two disagree is
  refused rather than quietly relabelled, because only the caller can settle which of them is
  wrong. The check runs when an upload capability is minted, so an unreadable format costs one
  round trip instead of a whole upload, and again on the delivered bytes before anything is scanned
  or stored. PDF, the image formats PNG, JPEG, GIF, TIFF, BMP and WebP, WAV audio, EPUB, the OOXML
  documents and the textual family are accepted. Video is refused deliberately, since aizk has no
  video reader and accepting it would store an opaque object nobody could search.

- `--profile observability` starts Alloy, Loki, Tempo, VictoriaMetrics and Grafana, and the three
  signals link to one another inside one pane. Every aizk process exports OTLP to Alloy rather than
  to Tempo directly, so a Tempo restart costs a retry in one place instead of dropped spans in
  every process, and Tempo's metrics generator turns those same spans into RED series and a service
  graph without a hand-rolled counter anywhere in the application. `CallerAnnotator` stamps the
  caller, the operation and the touched scopes onto every span opened under a request rather than
  only the first, which is what makes per-user cost answerable when a model call opens its own span
  long after the transport identified who asked. Model turns emit GenAI convention spans carrying
  the model, the token counts and the latency, while prompts and completions are deliberately
  excluded, since a chunk of somebody's private memory is exactly what they would contain.

- A third Caddy site on 8083 accepts OpenTelemetry over HTTP from outside the deployment, so a
  provider that emits its own generation traces, OpenRouter among them, can push them into that
  same Alloy and Tempo pipeline. It is the only public route no browser ever visits, so it
  authenticates on a bearer token instead of the operator gate in front of Grafana, which can only
  answer a client capable of following a redirect to a sign-in page. `AIZK_OTLP_TOKEN` carries no
  default and Compose refuses to start the `web` container without one, because Caddy substitutes
  the token when it parses the file rather than reading it per request, and an absent value would
  leave the matcher comparing against a bare `Bearer ` and admitting every caller. Alloy already
  owns `/v1/traces`, `/v1/metrics` and `/v1/logs` on that port, so the path passes through
  untouched, and the site declares no `depends_on`, which keeps a stack brought up without the
  observability profile answering 502 rather than refusing to start.

### Changed

- One source now stands for at most one promoted copy per destination scope set, enforced by the
  new `uq_document_promotion_scope` partial unique index and a transaction lock, so two concurrent
  shares can no longer both insert a destination. Migration `0006_document_promotion_identity`
  installs it on PostgreSQL and `0002_document_promotion_identity` does the same on the separate
  CockroachDB branch, both sorting existing scope arrays first so the index means set identity,
  and both refusing to run while duplicate copies stand rather than leaving the build to fail
  without naming them. The index also covers the `promoted_from` lookup every share performs.
  Sharing now batches its source, standing-copy and fact loads into one statement each and claims
  entities and facts through the batch APIs rather than one statement per row.
- Revising a document and sharing it now queue on one lock named for the document, so a move can
  no longer copy spans that a concurrent re-ingest has already replaced and then retire the source
  holding the newer text. A batch also claims every stored original it will reach in one sorted
  call before it starts, so two batches touching the same originals in opposite order cannot
  deadlock. Refreshing a copy never inherits a retired source's expiry onto a live destination,
  which would otherwise have retired the very copy the refresh brought up to date.
- Recall packing prices the annotation lines it renders, so the document and resource lines an
  evidence item carries are charged against the caller's token budget instead of overrunning it.
- Recall packing fills the budget greedily in merit order instead of taking the longest prefix.
  One oversized excerpt used to end the walk and discard everything ranked behind it, so a few
  long source spans could spend a whole budget while short, well-ranked evidence was never
  considered. Such an item is now stepped over and the walk continues, still in rank order and
  still deterministic. A budget too small for even the best item returns that item trimmed with
  a visible marker rather than returning nothing, its annotations being the floor it cannot go
  under.
- Recall drops a source excerpt whose span better-ranked evidence already speaks for, so an excerpt
  and a fact distilled from it no longer both spend the budget saying one thing twice. Only the
  excerpt side is ever dropped. A fact is one distinct statement and a span commonly yields several,
  so every fact stands and an excerpt that outranks the facts from its span is kept along with them,
  mild redundancy being the cheaper mistake than discarding statements. Nothing is weighed against a
  different document, and a community or overview summary names no span and so is never dropped,
  which keeps the mix of source excerpts and derived memories in a packed result intact.
- Recall's chunk lanes rank inside a `MATERIALIZED` window over `chunk` alone and join `document`
  outside it. Ranking chunks joined to their documents looks harmless and is not, because under row
  security the planner abandons both chunk indexes, loops over every visible chunk and sorts the
  lot, which on a production snapshot of 22,290 chunks cost 80 ms for the dense ranking and 245 ms
  for the lexical one and left the two largest indexes in the database, a 66 MB `vchordrq` and a
  343 MB `bm25`, maintained on every write and read by nothing. That same snapshot now ranks dense
  in 7.5 ms and lexical in 10 ms, both walking their index, and the whole fused statement went from
  320 ms to 21 ms with byte-identical output over nine query and vector combinations.
  `MATERIALIZED` is load bearing, since without it the planner folds the window back into the join
  and rebuilds the very scan this removes. Row security already hides every chunk whose document
  the caller cannot read, so the window sees exactly what the join would have seen and the join
  keeps only `Document.is_active()`, which the window pays for by reaching `fusion_overfetch` times
  deeper, three by default. Five of the 1,168 documents in that snapshot carry an expiry and one
  had expired, so three deep never comes close to spending the slack. An `owned` query is the
  exception and keeps its join inside the ranking, because an exact scope predicate is selective
  enough to drive the plan on its own while a global window would spend its over-fetch on documents
  the selection could never carry, which on a scope holding 2 percent of the corpus returned 31 and
  10 rows where the joined shape returned the full 50 in 4.7 ms and 10.8 ms.
- Two VectorChord knobs are now stated on every application connection rather than inherited from
  whatever the server was built with. `bm25_catalog.bm25_limit` follows the new `bm25_limit`
  setting, 150, which caps how many rows one bm25 index scan returns whatever the query asked for,
  and that cap is silent, so pinning it is what turns the lexical window into a promise the index
  keeps. The new `lexical_window_fits_bm25_limit` validator refuses to start when `fusion_depth`
  times `fusion_overfetch` exceeds it, rather than letting the lane be quietly cut short.
  `vchordrq.prefilter` follows the new `vchordrq_prefilter` setting and is off, where it used to be
  hardcoded on. Applying row security inside the ANN walk pays only when a caller reads a small
  share of the index, and one scope holds 98 percent of the chunks in the deployment measured here,
  so prefiltering cost 5,271 buffers against 2,465 with it off. Turn it on once many organizations
  each read their own small slice, and confirm it with `EXPLAIN (ANALYZE, BUFFERS)` on a real
  caller's dense ranking with the setting both ways.
- The store keeps less on disk and reads far less to answer a question. Docling is no longer asked
  for its native document tree at all, since nothing ever read it and a large PDF answers with tens
  of megabytes of JSON that would be parsed once and dropped, so `artifact_content` loses both
  `docling_json` and the conversion diagnostics in `details`. Both are reproducible by converting
  the original again. `markdown` stays because it is now load bearing rather than a derivative
  nothing consumes, being the text the re-chunk sweep replays. Every remaining site that needed
  only a scope array or a status stopped loading the whole row beside it, which is what detoasting
  the Markdown in that row costs. A conversion state transition, the conversion write itself, the
  reconversion sweep's candidate window and the browser artifact dashboard each select their own
  handful of columns now, and the dashboard in particular used to decompress megabytes of stored
  Markdown to render status lines that never show a word of it.
- PostgreSQL stores embeddings as `halfvec`, halving both embedding bytes and ANN index size, and
  every lane bind carries the same half precision type, because a bind that does not makes the
  planner cast the column side of the distance comparison and the index walk degrades to a scan.
  CockroachDB has no half precision vector type, so that branch keeps the portable full `VECTOR`
  column and has nothing to convert.
- The `db` container states two compression settings it used to leave at their defaults.
  `default_toast_compression` is `lz4`, which is faster both ways and usually denser than pglz on
  the large text columns this schema is full of, and PostgreSQL admits only those two here so it is
  the whole of the available choice. `wal_compression` is `zstd`, which that setting does admit,
  and full page images dominate WAL volume on a store that never stops writing through chunk
  inserts, embeddings and queue churn. Neither rewrites anything, so WAL applies to every segment
  written afterward and TOAST only to new values, with old rows keeping pglz until something
  rewrites them. `wal_buffers` rises to 128 MB after production recorded 216,229 waits on a full
  WAL buffer, each one a backend stalling to flush before it could continue, and `max_wal_size`
  rises to 24 GB after 76 percent of checkpoints turned out to be WAL pressure driven rather than
  timed with 21 percent of WAL records full page images. All of these sit on the command line, so
  they need the container recreated rather than reloaded.
- The MCP verbs are `find` and `keep` rather than `recall` and `remember`. `find` answers from
  memory first, always, and reaches the public web only for what memory could not answer and only
  after the question has been rewritten so that nothing identifying the asker leaves the machine,
  with every answer ending in a receipt naming exactly what left. `web` takes `auto`, `off` or
  `force` and `fresh` bypasses every cache, though neither overrules the stop that keeps a question
  about the asker's own notes, people, projects or machines from being planned at all. A fetched
  page is cached as an expiring document inside the caller's scopes, never enters the knowledge
  graph and always renders under the web label. Planning that rewrite is itself egress, since the
  question and a memory excerpt go to the configured extraction endpoint, so
  `AIZK_WEB_SEARCH_ENABLED` refuses to start unless that endpoint sits inside the deployment or is
  pinned to zero data retention.
- A derived fact carries how settled its source was, on a ladder running from `settled` through
  `reported`, `hedged` and `disputed` to `refuted`, and anything other than settled prints in the
  bracket beside the claim. Extraction proposes the mark and a deterministic check then re-reads
  the sentence behind the quote and compares it against what the claim says, and that check can
  only ever move a claim down the ladder, never up. It refuses a fact outright when the fact states
  more certainly than its own sentence did, so either the qualification survives into the fact or
  there is no fact. An answer holding an unsettled claim opens with a line telling the reader not
  to repeat it as fact, and the source excerpt behind such a claim stops counting as a repetition
  of it, so the sentence actually worth reading arrives beside the claim instead of being dropped
  as redundant.
- Container pins move forward, `oauth2-proxy` to v7.15.3, `cloudflared` to 2026.7.3, Grafana to
  13.1.2, Loki to 3.7.5, Tempo to 2.9.4, and ClamAV to a newer digest of the same
  `1.5.3-debian13-slim` tag. The three vLLM lanes deliberately stay on v0.24.0, which is the
  version that wrote every vector currently in the index. v0.26.0 is wanted for the missing device
  sync in the pooling path, which fixes wrong LAST-pooling scores when a prompt is split across
  prefill chunks under `torch.compile` and whose regression test drives the very
  `Qwen3ForSequenceClassification` reranker overrides this deployment already sets. The same span
  of releases also made Model Runner V2 the default execution path for dense models, so the
  embedder and the reranker both run through new code even where no numeric change was intended.
  That is why the move is gated rather than automatic. Starting clean is not the acceptance test,
  because embeddings written by a runtime that produces different vectors corrupt a 150k vector
  index silently with no error anywhere, and a redeploy is exactly the moment that would pass
  unnoticed, so the bump waits on a parity run comparing the two over a fixed probe set and passing
  only at cosine similarity at or above 0.9999 with unchanged rerank ordering. A failed parity run
  means re-embedding the corpus, which is a deliberate decision worth making on its own evidence
  and never a side effect of a redeploy. Holding here also means a redeploy does not recreate these
  containers at all, so no model reload interrupts serving.

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
- The embedding default was measured on a sanitized retrieval fixture rather than assumed.
  `Qwen3-VL-Embedding-2B` stayed close to the larger text-only model while retaining the image
  lane, so the multimodal default and the Matryoshka cut both stand. A text-only deployment can
  still swap the checkpoint and reembed.
- Facts are grounded to their exact source spans: the extraction schema asks each fact for
  the shortest verbatim supporting quote, and the graph writer aligns it to the chunk text
  (exact first, then case- and whitespace-insensitive with an offset map that survives
  multi-character casefolds like ß to ss) into `quote_start`/`quote_end` claim attributes.
  A quote that cannot be aligned grounds nothing rather than guessing. A bounded synthetic
  fixture confirmed that supported facts recover their source spans. The idea
  came from evaluating Google LangExtract head-to-head, which lost to the house extractor
  on yield, latency, and vocabulary enforcement but demonstrated char-interval grounding
  worth stealing.
- GLiNER2 moved behind one required GPU sidecar whose routes cover classification, mentions,
  relevance, and grounded graph extraction. The server process never imports torch, and an
  unavailable model fails visibly. `AIZK_EXTRACT_BACKEND` selects the production LLM extractor or
  the experimental GLiNER graph route without changing graph-building code. The service batches
  overlapping word windows through GLiNER2's public `batch_extract` API and restores source spans.
  A controlled GPU host comparison found the large checkpoint nearly as fast as base and somewhat
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
  load-bearing since unscaffolded requests can rank irrelevant text above answers. A sanitized
  reranking fixture showed that the small checkpoint degraded ordering while the 4B checkpoint
  preserved it, so 4B is the shipped default.

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

- The artifact derivative drop would have aborted on any deployment holding data. `details` is
  `NOT NULL`, and the first draft blanked both derivatives to `NULL` before dropping them, so the
  blanking `UPDATE` violated that constraint on the very rows it existed to clear and took the
  whole migration down with it. `0008_storage_footprint` blanks it to an empty `jsonb` instead,
  which still rewrites each row without the old value and still releases the out of line bytes.
  That blanking pass is itself the point, because dropping a column is a catalog edit. PostgreSQL
  marks the attribute dead and stops returning it while every existing row keeps the value on disk
  and every out of line value keeps its rows in the TOAST relation, so a bare `DROP COLUMN`
  reclaims nothing at all.

- Every conversion now names its OCR engine and languages, because the default read Chinese. aizk
  sent `do_ocr=true` and nothing else, so Docling chose RapidOCR, whose bundled recognition model
  set is Chinese and which maps requested languages onto only english, latin and chinese with no
  Japanese set at all. Every scanned or image-region Japanese page therefore came back as
  plausible but wrong CJK, and chunking, embedding and extraction all accepted it. `DoclingOptions`
  now sends `ocr_engine`, `ocr_preset` and `ocr_lang` on every request, defaulting to `tesseract`
  with `["jpn","eng"]` through `AIZK_DOCLING_OCR_ENGINE` and `AIZK_DOCLING_OCR_LANGUAGES`, and it
  refuses an empty language list because an empty list restores the engine's own default.
  `reconvert_scanned_documents` requeues every ready PDF and image so a corrected engine rewrites
  what the wrong one read, and it shares one `ReconversionSweep` with the web-page sweep.

  **Deploy requirement.** `quay.io/docling-project/docling-serve-cpu:v1.26.0` ships the tesseract
  binary and `tesserocr` but only `eng.traineddata` and `osd.traineddata`. Asking it for `jpn+eng`
  loads `eng` alone, logging `Failed loading language 'jpn'` while returning no error, so a
  deployment that reads Japanese must extend the image, `RUN dnf install -y
  tesseract-langpack-jpn`, or mount `jpn.traineddata` into `/usr/share/tesseract/tessdata/`. Until
  then Japanese scans are read as English, which is wrong but visibly wrong instead of plausibly
  wrong. EasyOCR is not an alternative at roughly seventy seconds per image on CPU. Run the
  reconversion sweep only after the image carries the language data.

- A preserved web page no longer floods recall with its own navigation. Converting an HTML page
  kept the site header, menus, dialogs, and footer beside the article, and a GitHub project page
  answered a question about a project plan with three chunks of sign-in links and pricing menus.
  `WebBoilerplateCleaner` now runs inside `ArtifactProcessor.declutter`, after source-relative
  links resolve and before the Markdown is stored or chunked, and only for an HTML original
  fetched from an HTTP source URI. It measures every blank-line separated block by the characters
  a reader actually reads and drops one only when prose value, link density, destination site,
  block size and the page's own layout all say chrome, so a paper's reference list, a curated row
  of external links and a documentation index of internal links all survive while a menu, a badge
  row, a repeated link block or anything under a `Footer` heading goes. A chrome heading discards
  its section only until the first block that reads like content, one sentence being enough, so a
  short article introduction under a menu is never eaten.
  `AIZK_ARTIFACT_BOILERPLATE_REMOVAL_ENABLED=false` restores the raw conversion, and
  `reconvert_web_pages` requeues pages converted before this landed so their stored text and
  chunks are rewritten, committing each page's move back to `queued` before its task exists so a
  worker that finishes mid-sweep is never overwritten.
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
- The revisions written past `0007_web_egress` and never deployed are squashed into one
  `0008_storage_footprint`, replacing the separate `0008_halfvec_storage` and
  `0009_blob_encoding_level`. It retypes every embedding column and rebuilds its ANN index as
  `halfvec`, adds the `blob.encoding_level` marker with its range check and index, blanks and drops
  the artifact derivatives, and adds the `indexed_at` re-chunk cursor. The `live_fact` view pins
  the column types of what it selects, so the revision drops and rebuilds it around the fact
  embedding retype. `0004_storage_footprint` is the CockroachDB counterpart and is shorter rather
  than incomplete, since that backend has no `halfvec` to convert to and stores a row's values
  inline with no TOAST relation, so there is nothing to blank and no `VACUUM FULL` to follow.
  Existing rows arrive with a null `encoding_level` and a null `indexed_at`, which read as an
  unknown compression policy and an unknown chunking policy and put them at the front of the first
  `aizk admin storage compact` and `aizk admin data rechunk` passes. Handing the freed file space
  back to the operating system is separate and worth one `pg_repack -t artifact_content` after the
  migration runs, which also rewrites that table's TOAST under the new `lz4` setting.

## 0.0.1

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
  maintenance and admin, with Logto identity.
- The eval harness, hit@k/nDCG@k/MRR scoring, a config sweep, and EverMemBench/TEMPO dataset
  loaders gated behind `AIZK_BENCHMARKS_ENABLED`.
- Documentation in the repository, with the engine explained in five parts,
  a paper-by-paper provenance map, and measured benchmarks and comparisons.
