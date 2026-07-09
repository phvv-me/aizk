import os
import uuid
from pathlib import Path
from textwrap import dedent
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# the package's own .env, four levels above this file (config/ -> aizk/ -> src/ -> the package
# root docker-compose.yml and .env.example already live in), resolved from this file's own
# location rather than the process cwd so `Settings()` finds it the same way whether the caller
# runs from the repo root, from packages/aizk, or from inside an installed wheel with no .env
# beside it at all. Passing a path that does not exist is a silent no-op for pydantic-settings, so
# no existence check is needed here; a real deployment with no .env keeps every field's own
# default, and compose's own `${VAR:-default}` fallbacks agree with them field for field.
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

# long prompt-text defaults, module-level so the field list below stays scannable and each
# field's docstring line stays a one-liner describing what the prompt steers rather than repeating
# its own body.
COMMUNITY_SUMMARY_SYSTEM_PROMPT = (
    "You summarize one cluster of a knowledge graph. Given the cluster's entities and the facts\n"
    "among them, write a short label naming the theme and a one-paragraph summary of what the\n"
    "cluster is about. Ground every word in the facts shown, never invent detail, and write the\n"
    "summary so a reader asking a broad question about this area would recognize it as relevant."
)
CONSOLIDATION_PROMPT = (
    "You maintain a bi-temporal knowledge graph. A non-LLM cascade already resolved every new\n"
    "fact whose similarity to an existing fact was unambiguous; you only see the genuinely\n"
    "borderline ones, numbered, each with its own catalog of similar existing facts. For each\n"
    "numbered item decide one action.\n"
    "ADD when the new fact states something none of its own existing facts cover.\n"
    "UPDATE when the new fact supersedes one of its own existing facts, such as a changed value\n"
    "or status, and name that fact's id in supersedes.\n"
    "NOOP when one of its own existing facts already states the same thing.\n"
    "Return exactly one verdict per numbered item shown, in the same order."
)
CURATION_REVIEW_SYSTEM_PROMPT = (
    "You are the standing reviewer for one curated group's shared memory. Given the group's\n"
    "already-approved canon and a queue of pending claims awaiting review, judge each pending\n"
    "claim on its own. Approve a claim that is consistent with the canon, adds real\n"
    "information, and reads as a well-formed fact. Reject a claim that contradicts the canon\n"
    "without evidence, restates something the canon already states, or reads as malformed or\n"
    "unsupported. Return exactly one verdict per pending claim shown, in the same order, each\n"
    "naming the claim id it judges and a one-sentence reason grounded only in the canon shown."
)
EXTRACT_PREFERENCES_PROMPT = (
    "Extract the durable preferences, decisions, and habits the text reveals about a person or a\n"
    "project, never the transient facts. Prefer Decision, Pattern, and Gotcha entities and the\n"
    "because, avoids, and uses relations that record why a choice holds, so the graph captures\n"
    "how the subject prefers to work rather than what a document happens to state."
)
EXTRACT_SUMMARY_PROMPT = (
    "Extract only the few highest-level entities the span is about and the claims that summarize\n"
    "it, never the incidental details. Prefer Concept, Claim, and Result entities and the\n"
    "relations that connect the span's main subject to what it asserts, so the graph reads as a\n"
    "summary of the span rather than an exhaustive transcription."
)
EXTRACT_SYSTEM_PROMPT = (
    "Extract only the entities and facts the document text actually asserts. Never describe this\n"
    "prompt, the ontology, the extraction task, or the json format as if they were content.\n"
    "Write every entity name and fact statement in English, whatever language the source text\n"
    "uses, and name the author of a first-person statement by role, the author, never a bare\n"
    'pronoun such as "I".\n'
    "Write every entity name as a plain human-readable noun phrase, never a slug, file name,\n"
    "kebab-case token, or code identifier, so team-memory-spine becomes team memory spine.\n"
    "Choose the single entity type that most precisely fits the thing, and when nothing fits\n"
    "use Concept rather than forcing an unrelated type. Each fact must read true as subject\n"
    "predicate object, and its statement must stand on its own without the surrounding text.\n\n"
    "Example.\n"
    'Text: "The team-memory-spine project uses Graphiti for bi-temporal storage, building on the\n'
    'work of the Zep authors."\n'
    "Entities: team memory spine (Project), Graphiti (Tool), Zep (Paper).\n"
    "Facts: team memory spine uses Graphiti; Graphiti extends Zep."
)
INSIGHT_SYSTEM_PROMPT = (
    "You study the facts already recorded about one graph and derive higher-level observations\n"
    "they jointly support. Write only observations grounded in the facts shown, never restating\n"
    "a single fact and never inventing detail beyond them, and score each by how much it adds\n"
    "over the facts it rests on. Prefer a few significant patterns to many shallow restatements."
)
ONTOLOGY_PROMPT_TEMPLATE = dedent(
    """
    Extract a knowledge graph using only the controlled vocabularies below.

    Entity types ({entity_count}):
    {entity_types}

    Relation types ({relation_count}):
    {relation_types}

    Rules.
    Use only the entity types and relation types listed above, never invent new ones.
    Every fact is a subject entity, a relation type as the predicate, and an object entity.
    Write each entity name in its canonical singular form, lowercase unless a proper noun.
    Write a one-sentence statement for each fact that stands on its own.
    Drop any candidate fact whose predicate is not in the relation list.
    """
)
PROFILE_SYSTEM_PROMPT = (
    "You write a short profile of one entity from the facts about it. Open with the stable,\n"
    "static identity of the thing, what it is and what it is for, then add the dynamic state the\n"
    "latest facts assert, its current status, values, and relations. Ground every word in the\n"
    "facts shown, never invent detail, and write one tight paragraph a reader could lift whole."
)
RAPTOR_ROLLUP_SYSTEM_PROMPT = (
    "You merge several cluster summaries that sit one level below into a single higher-level\n"
    "summary. Given the child summaries, write a short label naming the broader theme they share\n"
    "and a one-paragraph summary of what that theme covers. Ground every word in the child\n"
    "summaries shown, never invent detail, and write so a reader asking a broad question about\n"
    "this whole area would recognize it as relevant."
)


class Settings(BaseSettings):
    """Runtime configuration read from AIZK_-prefixed environment variables.

    admin_database_url: async DSN for the owning role `aizk_admin`, migrations and the cross-tenant
        maintenance passes. Defaults from host/port/db/`admin_password`. Override wholesale for a
        deployment shape the template can't express.
    admin_password: password for the owning role `aizk_admin`, folded into `admin_database_url`'s
        default. The role name itself is not a setting, `docker-compose`'s `POSTGRES_USER` and the
        `APP_ROLE` constant in `store/rls/ops.py` hardcode the role names both DSNs connect as, so
        only the password varies deployment to deployment.
    anon_rate_per_second: token-bucket rate anonymous HTTP callers may call tools at. Authenticated
        users pass unthrottled.
    backup_cron: crontab the scheduled `BackupTask` dumps the whole database on, daily before dawn
        by default, only fired when `backup_enabled` is set.
    backup_database_url: libpq URL the `backup`/`restore` tools connect with, empty to use
        `admin_asyncpg_dsn` (the host-mapped owner DSN). Set to the container-internal owner URL
        (Postgres on its own `5432`, not the host-mapped port) when `pg_client_launcher` runs the
        tools inside the database container.
    backup_dir: directory the scheduled `BackupTask` writes its timestamped dumps to, a mounted
        volume in the container so backups outlive it. Required when `backup_enabled` is set.
    backup_enabled: whether the worker registers the `BackupTask` cron at all, off by default so a
        plain host run never dumps, on in the container which mounts `backup_dir` and enables it.
    backup_keep_days: age past which the scheduled backup prunes an old dump, so the directory
        does not grow without bound.
    anonymous_user_id: all-zero identity an unauthenticated caller acts as, reading only
        public scopes. Fixed, not configurable, since the moat predicate's `::uuid` cast depends
        on every unscoped session binding this exact value.
    app_password: password for the restricted app role `aizk_app`, folded into `database_url`'s
        default. See `admin_password` for why the role name itself stays a fixed constant.
    auto_setup: whether the MCP server runs `ops.setup` automatically on startup when its own
        health check finds the schema behind head, so the server comes up ready with no manual
        migrate step. Off lets a deployment that wants manual control skip the auto-migrate.
    benchmarks_enabled: whether the admin benchmark tool may load the external EverMemBench and
        TEMPO eval datasets, an optional dev download.
    bm25_backend: lexical lane, `vchord_bm25` (default, VectorChord BM25) or `tsvector` (Postgres
        full-text fallback). Only vchord_bm25 builds the bm25vector column and its `<&>` index.
    chunk_denylist: comma-separated `identify` tags the code chunker treats as prose, parsed by
        `chunk_denylist_languages`. A denylist not an allowlist, so a new language routes to the
        code lane with no edit here.
    chunk_size: target characters per chunk, honored by both the prose and code chunkers.
    communities_cron: crontab the community fan-out fires on, gated per user by
        communities_every_n_facts so a quiet graph is not re-summarized.
    communities_enabled: whether the scheduler fans community detection out across users.
    communities_every_n_facts: facts a user's graph must gain since the last community build
        before a rebuild.
    community_backend: networkx graph backend Louvain detection runs on. A registered accelerator
        like cugraph can swap in at no code change.
    community_min_size: smallest entity count a detected community must reach to be summarized.
    community_summary_system: system prompt the community pass uses to name a cluster's theme and
        summarize it from its member entities and facts.
    consolidation_auto_merge_threshold: cosine similarity at or above which the non-LLM
        consolidation cascade decides a candidate fact's verdict by rule alone, no LLM call.
    consolidation_borderline_floor: cosine similarity below which a candidate fact's top similar
        claim is too dissimilar to be about the same thing, a trivial ADD by rule. A top match
        between this floor and consolidation_auto_merge_threshold is the genuinely ambiguous band
        the batched borderline LLM call decides instead.
    consolidation_prompt: system prompt the batched borderline-consolidation pass uses to decide
        ADD, UPDATE, or NOOP for each ambiguous fact against its own existing latest facts.
    context_token_budget: default token ceiling the context pack fills to, stopping before the
        next line would cross it.
    contextual_bm25: whether an ingested chunk prepends its document title to the text the lexical
        bm25/tsvector lanes index (the Anthropic contextual-retrieval lever). Only the lexical
        column carries the preamble. The dense embedding and displayed text stay the raw span.
    curation_review_canon_k: how many of a curated group's approved claims ground the review
        pass's judgment of its pending queue, the only material the judge may reason over.
    curation_review_cron: crontab the curation-review fan-out fires on, weekly after insight.
    curation_review_enabled: whether the scheduler fans the curation-review pass out across
        users.
    curation_review_system: system prompt the curation-review pass uses to approve or reject each
        pending claim against a curated group's visible canon.
    database_url: async DSN for the restricted app role row level security is enforced under.
        Defaults to the host/port/db/credentials template. Override wholesale
        (`AIZK_DATABASE_URL`) for a deployment shape the template can't express.
    db_host: hostname of the Postgres server both DSN templates default against.
    db_name: database name both DSN templates default against.
    db_null_pool: use NullPool instead of a real connection pool for the app-role engine. The
        pytest suite's `conftest.py` sets this since many tests each spin their own asyncio event
        loop and a pooled asyncpg connection cannot cross loops. Production wants the real pool.
    db_pool_max_overflow: extra connections the app-role pool opens above `db_pool_size` under
        burst load before a checkout blocks, only relevant when `db_null_pool` is off.
    db_pool_size: steady-state connections the app-role pool keeps open, only relevant when
        `db_null_pool` is off. A pooled checkout costs no new TCP/TLS handshake, the 20-34ms a
        fresh `NullPool` connection paid per call.
    db_port: port of the Postgres server both DSN templates default against.
    decay_cron: crontab the decay fan-out fires on, daily before dawn by default.
    decay_enabled: whether the scheduler fans the daily decay pass out across users.
    decay_floor: relevance floor a latest fact must clear to stay in the live graph. An untouched
        fact holds 0.5 relevance at one half-life and 0.25 at two, so this floor forgets facts
        unreached for roughly two half-lives. A single access lifts a fact back above it.
    decay_half_life_days: age in days at which an unaccessed fact's relevance halves, the decay
        pass's forgetting rate.
    dedup_cron: crontab the dedup fan-out fires on, nightly by default.
    dedup_enabled: whether the scheduler fans the nightly entity-dedup pass out across users.
    embed_api_key: bearer token for the embeddings endpoint, empty for a local server that ignores
        it.
    embed_batch_size: how many texts one /v1/embeddings request carries.
    embed_dim: embedding width stored as a pgvector halfvec. Drives both DDL column width and the
        `dimensions` truncation every embed request asks the Matryoshka checkpoint for.
    embed_instruction_document: instruction prepended to a stored document in the Qwen3-Embedding
        Instruct/Query wrapper, empty by default since the reference deployment embeds documents
        as plain text.
    embed_instruction_query: instruction every search query is wrapped in through the
        Qwen3-Embedding Instruct/Query prefix, steering the query vector toward answering passages.
    embed_model: served model name the embeddings endpoint answers to.
    embed_request_timeout: wall-clock ceiling on one embed HTTP request, text or image lane alike.
    embed_url: base URL of the OpenAI-compatible /v1/embeddings endpoint, the co-resident vLLM
        serving Qwen3-VL-Embedding by default.
    entity_resolution_threshold: cosine similarity above which a name reuses an existing entity.
    eval_judge: whether the eval harness asks the LLM to judge answerability beyond plain hit-at-k.
    eval_sample_questions: latest facts to sample into questions when the eval harness is given no
        question set, a small probe rather than an exhaustive sweep.
    extract_custom_prompt: extra guidance the custom extraction strategy layers on the ontology,
        empty to leave default ontology extraction in place.
    extract_max_tokens: hard cap on extraction/consolidation output tokens, a guard against
        unbounded generation.
    extract_min_chars: fewest characters of prose a chunk must carry before the build spends an
        extraction call on it. A shorter chunk is marked processed with no entities or facts, a
        heading or a stray line never worth the LLM round trip.
    extract_preferences_prompt: preferences strategy's focus, layered on the ontology prompt,
        steering toward durable choices and habits.
    extract_strategy: extraction strategy the build path runs, `ontology` (closed-vocab default),
        `summary`, `preferences`, or `custom`. Every strategy still validates against the closed
        ontology.
    extract_summary_prompt: summary strategy's focus, layered on the ontology prompt, steering
        toward the few highest-level entities and claims.
    extract_system_prompt: ontology default strategy's focus, the few-shot guidance keeping
        extracted entity names and facts well formed.
    extract_temperature: sampling temperature for extraction, zero for reproducible output.
    extract_timeout: per-call wall-clock ceiling on an extraction or consolidation generation. A
        runaway response is skipped rather than stalling the build.
    fusion_depth: candidate pool depth each of the dense and lexical chunk lanes contributes
        before reciprocal-rank fusion merges down to the requested k.
    gap_seed_terms: best already-recalled statements that seed the expanded gap-fill query, kept
        small so the extra round stays targeted.
    gliner_gate_device: torch device (or `map_location` value) the gliner2 gate loads on, `cpu` by
        default since the local GPU is already spoken for by the embed/rerank/llm vLLM trio.
    gliner_gate_enabled: whether the gliner2 relevance gate runs ahead of the combined extraction
        call at all. A chunk it clears skips the LLM call entirely.
    gliner_gate_floor: entity kinds whose presence alone never earns a chunk an LLM call, the
        pronoun-level catch-alls the classification head maps small talk onto (`Person` by
        default). A chunk clears the gate only when `classify_text` reports a type past this floor.
    gliner_gate_model: gliner2 checkpoint the gate loads, the 205M unified extraction model.
    gliner_gate_threshold: per-label confidence the classification head must clear for a type to
        count as present, the `cls_threshold` handed to `classify_text`. Swept against real prose,
        clear filler stayed under 0.6 while ontology-bearing text sat at 0.9+, so 0.7 rejects small
        talk with margin without ever dropping a substantive chunk.
    graph_build_concurrency: pending chunks a graph build extracts and consolidates at once, the
        `asyncio.Semaphore` width shared by `build_graph`'s inline loop and the pgqueuer worker's
        extraction entrypoint, so both paths hit the LLM endpoint at the same bounded concurrency
        regardless of how many chunk jobs pgqueuer itself has dispatched as concurrent tasks.
        Matches the compose vllm-llm service's own `--max-num-seqs` so this pipeline's own
        concurrency is what saturates vLLM's continuous batching, not a starved queue.
    graph_facts_k: number of latest facts retrieved per graph search.
    index_backend: vector index the halfvec columns use, `vchordrq` (default, RAM-frugal) or
        `hnsw` (portable fallback). Both share the cosine op. Drives both the migration DDL and
        the Embedded mixin's `embedding_index`, so they must agree.
    insight_cron: crontab the insight fan-out fires on, weekly after self-improve.
    insight_enabled: whether the scheduler fans the reflective insight pass out across users,
        deriving and writing back higher-level observations.
    insight_facts_k: latest facts the insight pass grounds its observations in, the only material
        it may reason over.
    insight_max: most observations the insight pass writes per run, capping a noisy model from
        flooding the graph.
    insight_min_significance: significance an observation must reach to be written back, keeping
        low-value self-talk out of the graph.
    insight_system: system prompt the insight pass uses to derive higher-level observations from
        the latest facts.
    llm_api_key: bearer token for the chat endpoint, defaulting to the ambient OPENAI_API_KEY. Set
        to a cloud provider's key (e.g. CEREBRAS_API_KEY) when pointing llm_url at one.
    llm_chat_template_kwargs: extra `chat_template_kwargs` merged into every `structured` call's
        `extra_body`, empty by default so a stock load sends none and never risks a hosted
        OpenAI-shaped provider rejecting an unrecognized field. The local vllm-llm deployment sets
        it to `{"enable_thinking": false}` to disable a hybrid-thinking model's own `<think>`
        preamble, which would otherwise burn the combined call's token budget on reasoning the
        closed ontology schema never asked for.
    llm_model: chat model id used to extract entities, facts, and dates in the combined call.
    llm_provider: label recording which provider llm_url points at (`vllm` local, `cerebras`
        hosted), surfaced in diagnostics. The url is the client's real source of truth.
    llm_request_timeout: HTTP-level ceiling on the `AsyncOpenAI` client request, generous so a
        slow local model isn't cut off mid-stream, while extract_timeout is the tighter per-call
        ceiling.
    llm_url: base URL of the OpenAI-compatible chat endpoint for graph extraction, local vLLM by
        default. Point at any OpenAI-compatible provider's url to switch (e.g. Cerebras).
    log_level: loguru sink level aizk's diagnostics are emitted at, empty to disable the logger
        entirely rather than filter it.
    louvain_seed: fixed seed for Louvain partitioning, so the same graph yields the same
        communities run to run.
    mcp_host: interface the HTTP MCP server binds when mcp_http is set.
    mcp_http: serve the MCP server over streamable HTTP instead of stdio.
    mcp_port: port the HTTP MCP server listens on when mcp_http is set.
    ontology_growth_threshold: cosine similarity at or above which the auto-create cascade folds
        an extractor's suggested type into an existing entity kind rather than minting a new one.
    ontology_prompt_template: ontology guidance every extraction strategy layers on, a
        `str.format` template with entity_count/entity_types/relation_count/relation_types
        placeholders `extract.ontology.cache.build_snapshot` fills from the live catalog.
    pg_client_launcher: command prefix the `backup`/`restore` tools run `pg_dump`/`pg_restore`
        through, empty to run the host's own binaries. The client must be at least the server's
        version, so a compose deployment sets this to run the tools inside the database container
        whose binaries match by construction, e.g. `["docker", "exec", "-i", "aizk-db-1"]`.
    ppr: whether recall expands the seed facts through personalized pagerank for multi-hop reach.
    ppr_alpha: damping for personalized pagerank, the chance of following an edge over teleport.
    ppr_margin: cosine-similarity floor a multi-hop pagerank fact must clear before recall folds
        it in, dropping structurally central but off-topic hub facts a broad query's walk surfaces.
    ppr_max_fanout: most neighbors the bounded walk expands per node per hop, capping one hub
        entity from pulling in the whole graph.
    ppr_max_hops: how many hops the bounded local walk follows out from the seeds before stopping.
    default_user_id: identity the MCP server and hook commands act as until an auth seam resolves
        one, the single-user stdio default.
    profiling: whether `mainboard.profiling.span` recording is turned on at startup, off by
        default so an unmeasured deployment pays only the one boolean check each span reads. The
        `serve_mcp` and `worker` entrypoints call `enable_spans()` once when this is set. The
        admin `profile_report` tool reads the process-wide `Collector` it feeds.
    profile_on_write: whether a finished extraction enqueues a debounced profile rebuild for each
        touched entity, refreshing its portrait without waiting for the weekly pass.
    profile_refresh_cron: crontab the full profile refresh fires on, weekly by default.
    profile_refresh_enabled: whether the scheduler fans the weekly full profile refresh out.
    profile_system: system prompt the profile pass uses, opening with an entity's stable identity
        then its dynamic state from the latest facts.
    profiles: whether recall surfaces the static-plus-dynamic profile of its top matched entity.
    promoted_bonus: additive score bump the chunk-lane fusion gives a hit whose document carries
        promote provenance, letting audited/admin-published knowledge outrank an equally-ranked
        unpromoted hit.
    queue_batch_size: pgqueuer jobs `aizk worker` dequeues per round, sized above
        graph_build_concurrency so the queue path actually keeps enough chunks in flight to
        saturate vLLM's continuous batching rather than the dequeue width itself becoming the
        bottleneck a smaller extraction_semaphore would otherwise hide.
    query_routing: whether recall classifies a query as local/global/multi-hop and narrows the
        retrieval mix to that route, default-off until the eval A/B proves it beats the fixed mix.
    raptor: whether recall folds in RAPTOR summaries, root level for a broad query and leaf level
        for a pointed one.
    raptor_cron: crontab the RAPTOR fan-out fires on, weekly after communities.
    raptor_enabled: whether the scheduler fans the weekly RAPTOR tree build out across users.
    raptor_every_n_facts: latest facts a user's graph must gain since the last RAPTOR build
        before a rebuild.
    raptor_k: how many RAPTOR summaries recall folds into a query's context.
    raptor_max_levels: hard ceiling on levels the RAPTOR climb adds above the leaves, a second
        guard alongside the shrink-to-root stop.
    raptor_redundancy_threshold: cosine similarity above which a new RAPTOR summary counts as a
        duplicate at its level, the DTCRS prune that reuses the kept node.
    raptor_rollup_system: system prompt the RAPTOR level-rollup pass uses to name a broader theme
        and summarize it from several child summaries one level below.
    raptor_root_max: largest node count a RAPTOR level may hold and still be a root, the climb
        stopping once a level shrinks to at most this many summaries.
    raptor_sim_threshold: cosine similarity two summaries must reach to link when a RAPTOR level
        is clustered into the level above it, lower merging more aggressively into fewer parents.
    recall_gap_fill: whether a thin recall issues one targeted extra retrieval round before
        returning, default-on since the gap signal is a hit count rather than a model call.
    recall_gap_judge: whether the gap check also asks the LLM judge if the rendered context
        answers the query, off by default so the common path pays no model call.
    recall_gap_min_hits: fewest hits a recall may carry before it counts as an evidence gap worth
        one extra round.
    recall_gap_min_score: best-hit score a recall must clear or it counts as a gap. Zero disables
        the check, leaving only the hit-count floor.
    reembed_batch: how many rows one re-embed batch carries when `reembed` walks a stored table in
        bounded chunks after a model or width change.
    rerank: whether search reorders the fused candidates with a cross-encoder before truncating.
    rerank_api_key: bearer token for the api reranker endpoint, empty for a local server that
        ignores it.
    rerank_candidates: width of the fused pool handed to the reranker before keeping the top k.
    rerank_min_pool: fewest fused candidates a pool must carry before reranking pays for itself.
        A pool at or under this size skips the cross-encoder round trip entirely, since fusion's
        own order already reflects every candidate when there is nothing left to reorder or trim.
    rerank_model: served model name the /v1/rerank endpoint answers to, matching the co-resident
        vllm-rerank --served-model-name.
    rerank_request_timeout: wall-clock ceiling on one /v1/rerank HTTP request.
    rerank_snippet_chars: characters of each candidate's text the cross-encoder scores against the
        query, truncated before the /v1/rerank call since the endpoint's own latency scales with
        candidate length. The stored and returned hit text is never truncated, only what the
        reranker itself reads. A cross-encoder's relevance judgment is dominated by a passage's
        early tokens, so trading the tail of a long chunk for a materially faster rerank round
        trip costs little precision.
    rerank_url: base URL of the OpenAI-compatible /v1/rerank endpoint, the co-resident
        vllm-rerank container on its own port by default.
    rrf_k: reciprocal-rank-fusion smoothing constant shared by the chunk-lane and chunk-fact
        fusions.
    self_improve_cron: crontab the self-improve fan-out fires on, weekly by default.
    self_improve_enabled: whether the scheduler fans the weekly self-evaluation across users.
    serve_with_worker: whether `serve-mcp` also runs the background worker in its own process,
        gathered on the one event loop, so a single container is server, worker, and scheduled
        backup at once. On by default for that single-box case, set off to run the server alone
        beside a separate `aizk worker`, the split a horizontally scaled deployment wants so its
        extra server replicas never each fire the crons.
    self_improve_max_p: largest ranx significance p-value the weekly pass accepts before flipping
        a config axis, low by default so a noisy delta never flips it.
    session_promote_age_minutes: age in minutes after which an unpromoted session item is fed into
        the long-term graph, the aged half of the promotion trigger.
    session_promote_cron: crontab the session-promotion fan-out fires on, every quarter hour by
        default so working memory drains into the graph promptly.
    session_promote_enabled: whether the scheduler fans the session-promotion pass out across
        users, moving aged or overflow working items into the long-term graph.
    session_promote_threshold: most unpromoted items a user's working memory holds before the
        oldest beyond the cap are promoted regardless of age, the overflow half of the trigger.
    session_recall_k: how many still-working session items a recall folds in beside the graph,
        zero to leave the session tier out of recall entirely.
    similar_facts: similar latest facts of the same subject `graph.consolidation.rank_pool` keeps
        per candidate, the pool `decide_by_rule` and the batched borderline call choose ADD,
        UPDATE, or NOOP over.
    skip_live_gate: execution-option key opting a fact read out of the session listener's live
        gate, for reads that must see superseded rows (as_of replay, raw counts, promote-copy).
    snippet_chars: characters a hit or fact snippet is truncated to when rendered into a recall
        bundle or a context pack, the display-width budget both renderers share.
    system_user_id: identity that owns rows ingested before the visibility lattice, the
        owner-and-scope model every row's row level security policy compiles from, existed, and
        that a scheduled background pass acts as when a caller does not name a per-user one.
    oidc_client_id: client id of the aizk resource server registered at the issuer, the
        identity the introspection call authenticates as.
    oidc_client_secret: client secret paired with oidc_client_id for the introspection call,
        held by the resource server and never the caller.
    oidc_introspect_url: RFC 7662 introspection endpoint. Empty keeps the offline JWKS path,
        set to validate each token against the issuer instead, catching revocation before expiry.
    oidc_issuer: base issuer URL whose JWTs are accepted, empty to leave the OIDC path off.
    oidc_jwks_url: JWKS endpoint the issuer publishes its signing keys at, to verify tokens.
    oidc_algorithm: JWS signing algorithm the offline JWKS path verifies against, `RS256` for
        most providers, `ES384` for Logto. A mismatch fails every signature with no error.
    oidc_groups_claim: access-token claim carrying the user's identity-provider organization
        memberships and roles, which `Group.sync_user_groups` reconciles the membership table to on
        each authenticated request. Empty leaves membership hand-managed, the default until the
        provider is configured to emit the claim.
    mcp_resource_url: this server's own public base URL, advertised in the RFC 9728 protected
        resource metadata so a client discovers the OIDC issuer and logs in through it, getting
        and refreshing its own tokens. Empty serves the bare token verifier with no advertising,
        the single-user default where the caller already holds a token.
    oidc_audience: the exact `aud` claim a token must carry to be accepted, the RFC 8707 resource
        indicator this server is. Empty derives it from `mcp_resource_url` (the advertised `/mcp`
        resource), so the public path validates audience by default and only an unusual provider
        whose `aud` differs from the resource url needs to set this. Without it, any token the same
        issuer signed for a different resource would be accepted here.
    oidc_required_scopes: comma-separated access-token scopes the resource both requires and
        advertises as `scopes_supported`, so a client requests exactly them and the provider mints
        a resource-audience token carrying them. A resource-indicator login grants only scopes the
        client asks for, so an empty value leaves the provider nothing to grant and it denies the
        authorization. `control` for the standard aizk resource whose one permission gates every
        client verb.
    """

    # extra="ignore": .env also carries compose-only container knobs (AIZK_EMBED_CHECKPOINT,
    # AIZK_EMBED_GPU_MEM_UTIL, and their rerank/llm counterparts) that vLLM reads and Settings
    # never does, so the dotenv loader must tolerate names with no matching field rather than
    # raising on them.
    model_config = SettingsConfigDict(env_prefix="AIZK_", env_file=ENV_FILE, extra="ignore")

    admin_database_url: str = ""
    admin_password: str = "aizk"
    anon_rate_per_second: float = 1.0
    anonymous_user_id: uuid.UUID = uuid.UUID(int=0)
    app_password: str = "aizk_app"
    auto_setup: bool = True
    backup_cron: str = "0 2 * * *"
    backup_database_url: str = ""
    backup_dir: str = ""
    backup_enabled: bool = False
    backup_keep_days: int = 14
    benchmarks_enabled: bool = False
    bm25_backend: str = "vchord_bm25"
    chunk_denylist: str = (
        "markdown,rst,asciidoc,tex,bib,plain-text,pofile,html,xml,dtd,css,scss,less,json,"
        "json5,jsonnet,yaml,toml,ini,java-properties,csv,tsv,cue,gitconfig,gitignore,"
        "gitattributes,editorconfig"
    )
    chunk_size: int = 2048
    communities_cron: str = "0 4 * * 0"
    communities_enabled: bool = True
    communities_every_n_facts: int = 50
    community_backend: str = "networkx"
    community_min_size: int = 3
    community_summary_system: str = COMMUNITY_SUMMARY_SYSTEM_PROMPT
    consolidation_auto_merge_threshold: float = 0.9
    consolidation_borderline_floor: float = 0.75
    consolidation_prompt: str = CONSOLIDATION_PROMPT
    context_token_budget: int = 2048
    contextual_bm25: bool = False
    curation_review_canon_k: int = 40
    curation_review_cron: str = "0 8 * * 0"
    curation_review_enabled: bool = True
    curation_review_system: str = CURATION_REVIEW_SYSTEM_PROMPT
    database_url: str = ""
    db_host: str = "localhost"
    db_name: str = "aizk"
    db_null_pool: bool = False
    db_pool_max_overflow: int = 10
    db_pool_size: int = 10
    db_port: int = 5433
    decay_cron: str = "0 3 * * *"
    decay_enabled: bool = True
    decay_floor: float = 0.25
    decay_half_life_days: float = 90.0
    dedup_cron: str = "30 3 * * *"
    dedup_enabled: bool = True
    embed_api_key: str = ""
    embed_batch_size: int = 32
    embed_dim: int = 1024
    embed_instruction_document: str = ""
    embed_instruction_query: str = (
        "Given a search query, retrieve relevant passages that answer it."
    )
    embed_model: str = "qwen3-vl-emb"
    embed_request_timeout: float = 120.0
    embed_url: str = "http://localhost:8000/v1"
    entity_resolution_threshold: float = 0.85
    eval_judge: bool = False
    eval_sample_questions: int = 10
    extract_custom_prompt: str = ""
    extract_max_tokens: int = 2048
    extract_min_chars: int = 80
    extract_preferences_prompt: str = EXTRACT_PREFERENCES_PROMPT
    extract_strategy: str = "ontology"
    extract_summary_prompt: str = EXTRACT_SUMMARY_PROMPT
    extract_system_prompt: str = EXTRACT_SYSTEM_PROMPT
    extract_temperature: float = 0.0
    extract_timeout: float = 90.0
    fusion_depth: int = 50
    gap_seed_terms: int = 2
    gliner_gate_device: str = "cpu"
    gliner_gate_enabled: bool = True
    gliner_gate_floor: frozenset[str] = frozenset({"Person"})
    gliner_gate_model: str = "fastino/gliner2-base-v1"
    gliner_gate_threshold: float = 0.7
    graph_build_concurrency: int = 48
    graph_facts_k: int = 20
    # vchordrq keeps the RaBitQ quantized codes in RAM and streams the full halfvec rows from SSD,
    # the RAM-frugal default. Flip both defaults to hnsw + tsvector for a managed Postgres that
    # ships no VectorChord, trading RAM headroom for the portable index.
    index_backend: str = "vchordrq"
    insight_cron: str = "0 7 * * 0"
    insight_enabled: bool = True
    insight_facts_k: int = 40
    insight_max: int = 5
    insight_min_significance: float = 0.6
    insight_system: str = INSIGHT_SYSTEM_PROMPT
    llm_api_key: str = Field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    llm_chat_template_kwargs: dict[str, bool] = {}
    llm_model: str = "gemma4-e2b-llm"
    llm_provider: str = "vllm"
    llm_request_timeout: float = 600.0
    llm_url: str = "http://localhost:8002/v1"
    log_level: str = "INFO"
    louvain_seed: int = 7
    mcp_host: str = "127.0.0.1"
    mcp_http: bool = False
    mcp_port: int = 8000
    ontology_growth_threshold: float = 0.85
    ontology_prompt_template: str = ONTOLOGY_PROMPT_TEMPLATE
    pg_client_launcher: list[str] = []
    ppr: bool = True
    ppr_alpha: float = 0.5
    ppr_margin: float = 0.35
    ppr_max_fanout: int = 50
    ppr_max_hops: int = 3
    default_user_id: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")
    profiling: bool = False
    profile_on_write: bool = True
    profile_refresh_cron: str = "0 5 * * 0"
    profile_refresh_enabled: bool = True
    profile_system: str = PROFILE_SYSTEM_PROMPT
    profiles: bool = True
    promoted_bonus: float = 0.01
    queue_batch_size: int = 64
    query_routing: bool = False
    raptor: bool = True
    raptor_cron: str = "30 4 * * 0"
    raptor_enabled: bool = True
    raptor_every_n_facts: int = 50
    raptor_k: int = 3
    raptor_max_levels: int = 5
    raptor_redundancy_threshold: float = 0.95
    raptor_rollup_system: str = RAPTOR_ROLLUP_SYSTEM_PROMPT
    raptor_root_max: int = 3
    raptor_sim_threshold: float = 0.5
    recall_gap_fill: bool = True
    recall_gap_judge: bool = False
    recall_gap_min_hits: int = 3
    recall_gap_min_score: float = 0.0
    reembed_batch: int = 128
    rerank: bool = True
    rerank_api_key: str = ""
    rerank_candidates: int = 50
    rerank_min_pool: int = 3
    rerank_model: str = "Qwen/Qwen3-Reranker-4B"
    rerank_request_timeout: float = 120.0
    rerank_snippet_chars: int = 320
    rerank_url: str = "http://localhost:8001/v1"
    rrf_k: int = 60
    self_improve_cron: str = "0 6 * * 0"
    self_improve_enabled: bool = True
    self_improve_max_p: float = 0.01
    session_promote_age_minutes: float = 60.0
    session_promote_cron: str = "*/15 * * * *"
    session_promote_enabled: bool = True
    session_promote_threshold: int = 20
    session_recall_k: int = 5
    serve_with_worker: bool = True
    similar_facts: int = 5
    skip_live_gate: str = "aizk_skip_live_gate"
    snippet_chars: int = 280
    system_user_id: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_introspect_url: str = ""
    oidc_issuer: str = ""
    oidc_jwks_url: str = ""
    oidc_algorithm: str = "RS256"
    oidc_groups_claim: str = ""
    mcp_resource_url: str = ""
    oidc_audience: str = ""
    oidc_required_scopes: str = ""

    @model_validator(mode="after")
    def default_dsns(self) -> Self:
        """Fill an unset `database_url`/`admin_database_url` from the host/port/db and passwords.

        This only fires when the field is still its empty default. An explicit value, whether a
        constructor kwarg or the `AIZK_DATABASE_URL`/`AIZK_ADMIN_DATABASE_URL` env override,
        always wins outright, the escape a cloud profile needs to point at a differently shaped
        deployment, TLS params or a managed host included, that the `db_host`/`db_port`/`db_name`
        template alone cannot express. The role names are the fixed `aizk_app`/`aizk` constants
        `admin_password`'s docstring explains, only the passwords come from settings.
        """
        if not self.database_url:
            self.database_url = (
                f"postgresql+asyncpg://aizk_app:{self.app_password}@{self.db_host}:{self.db_port}"
                f"/{self.db_name}"
            )
        if not self.admin_database_url:
            self.admin_database_url = (
                f"postgresql+asyncpg://aizk_admin:{self.admin_password}@{self.db_host}:{self.db_port}"
                f"/{self.db_name}"
            )
        return self

    @property
    def asyncpg_dsn(self) -> str:
        """The app-role `database_url` with the `+asyncpg` driver tag dropped.

        Every SQLAlchemy async DSN in this class carries the tag so `create_async_engine` picks
        the asyncpg driver, but pgqueuer's `AsyncpgDriver` dials asyncpg directly and chokes on it.
        """
        return self.database_url.replace("+asyncpg", "", 1)

    @property
    def admin_asyncpg_dsn(self) -> str:
        """The owner-role `admin_database_url` with the `+asyncpg` driver tag dropped.

        The migration counterpart of `asyncpg_dsn`, used only where a queue install or grant needs
        the owning role's direct asyncpg connection rather than SQLAlchemy's async engine.
        """
        return self.admin_database_url.replace("+asyncpg", "", 1)

    @property
    def app_role(self) -> str:
        """Name of the restricted role the app connects under, read from `database_url`."""
        return urlsplit(self.database_url).username or "aizk_app"

    @property
    def mcp_resource_id(self) -> str:
        """The RFC 8707 resource indicator this server is, the `aud` a valid token must carry.

        The `RemoteAuthProvider` advertises the protected resource at the `/mcp` mount under
        `mcp_resource_url`, and a resource-indicator login echoes exactly that into the token's
        `aud`, so the two must agree byte for byte. `oidc_audience` overrides it, empty when no
        public url is advertised and the single-user path presents a pre-issued token instead.
        """
        base = self.mcp_resource_url.rstrip("/")
        return self.oidc_audience or (f"{base}/mcp" if base else "")

    @property
    def chunk_denylist_languages(self) -> frozenset[str]:
        """The `chunk_denylist` comma-separated field, parsed into an immutable language set."""
        return frozenset(self.chunk_denylist.split(","))
