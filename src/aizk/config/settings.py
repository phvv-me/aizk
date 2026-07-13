import os
import uuid
from functools import cache
from pathlib import Path
from textwrap import dedent
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic.networks import AnyHttpUrl
from pydantic.types import UUID5, PositiveFloat, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.sql.selectable import Select

# Resolve the package environment independently of the process working directory.
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

_COMMUNITY_SUMMARY_SYSTEM_PROMPT = (
    "You summarize one cluster of a knowledge graph. Given the cluster's entities and the facts\n"
    "among them, write a short label naming the theme and a one-paragraph summary of what the\n"
    "cluster is about. Ground every word in the facts shown, never invent detail, and write the\n"
    "summary so a reader asking a broad question about this area would recognize it as relevant."
)
_CONSOLIDATION_PROMPT = (
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
_EXTRACT_PREFERENCES_PROMPT = (
    "Extract the durable preferences, decisions, and habits the text reveals about a person or a\n"
    "project, never the transient facts. Prefer Decision, Pattern, and Gotcha entities and the\n"
    "because, avoids, and uses relations that record why a choice holds, so the graph captures\n"
    "how the subject prefers to work rather than what a document happens to state."
)
_EXTRACT_SUMMARY_PROMPT = (
    "Extract only the few highest-level entities the span is about and the claims that summarize\n"
    "it, never the incidental details. Prefer Concept, Claim, and Result entities and the\n"
    "relations that connect the span's main subject to what it asserts, so the graph reads as a\n"
    "summary of the span rather than an exhaustive transcription."
)
_EXTRACT_SYSTEM_PROMPT = (
    "Extract only the entities and facts the document text actually asserts. Never describe this\n"
    "prompt, the ontology, the extraction task, or the json format as if they were content.\n"
    "Write every entity name and fact statement in English, whatever language the source text\n"
    "uses, and name the author of a first-person statement by role, the author, never a bare\n"
    'pronoun such as "I".\n'
    "Write every entity name as a plain human-readable noun phrase, never a slug, file name,\n"
    "kebab-case token, or code identifier, so team-memory-spine becomes team memory spine.\n"
    "Choose the single entity type that most precisely fits the thing, and when nothing fits\n"
    "use Concept rather than forcing an unrelated type. Each fact must read true as subject\n"
    "predicate object, and its statement must stand on its own without the surrounding text.\n"
    "Give each fact a quote, the shortest excerpt copied verbatim from the text that supports\n"
    "it, so the fact stays anchored to its exact source span.\n\n"
    "Classify each fact as world for objective shared state, experience for something a speaker\n"
    "did or encountered, observation for something a speaker perceived, opinion for a belief or\n"
    "judgment, preference for a durable choice, procedure for reusable steps, or negative_result\n"
    "for an attempted approach that failed. Keep the named speaker in every non-world\n"
    "statement.\n\n"
    "Example.\n"
    'Text: "The team-memory-spine project uses Graphiti for bi-temporal storage, building on the\n'
    'work of the Zep authors."\n'
    "Entities: team memory spine (Project), Graphiti (Tool), Zep (Paper).\n"
    "Facts: team memory spine uses Graphiti; Graphiti extends Zep."
)
_INSIGHT_SYSTEM_PROMPT = (
    "You study the facts already recorded about one graph and derive higher-level observations\n"
    "they jointly support. Write only observations grounded in the facts shown, never restating\n"
    "a single fact and never inventing detail beyond them, and score each by how much it adds\n"
    "over the facts it rests on. Prefer a few significant patterns to many shallow restatements."
)
_ONTOLOGY_PROMPT_TEMPLATE = dedent(
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
_PROFILE_SYSTEM_PROMPT = (
    "You write a short profile of one entity from the facts about it. Open with the stable,\n"
    "static identity of the thing, what it is and what it is for, then add the dynamic state the\n"
    "latest facts assert, its current status, values, and relations. Ground every word in the\n"
    "facts shown, never invent detail, and write one tight paragraph a reader could lift whole."
)
_RAPTOR_ROLLUP_SYSTEM_PROMPT = (
    "You merge several cluster summaries that sit one level below into a single higher-level\n"
    "summary. Given the child summaries, write a short label naming the broader theme they share\n"
    "and a one-paragraph summary of what that theme covers. Ground every word in the child\n"
    "summaries shown, never invent detail, and write so a reader asking a broad question about\n"
    "this whole area would recognize it as relevant."
)


class Settings(BaseSettings):
    """Runtime configuration read from AIZK_-prefixed environment variables."""

    # Compose adds vLLM variables that are outside this model.
    model_config = SettingsConfigDict(env_prefix="AIZK_", env_file=_ENV_FILE, extra="ignore")

    admin_database_url: str = ""
    admin_password: str = "aizk"
    anon_rate_per_second: float = 1.0
    anonymous_user_id: uuid.UUID = uuid.UUID(int=0)
    app_password: str = "aizk_app"
    auth_token: SecretStr = SecretStr("")
    auto_setup: bool = True
    backup_cron: str = "0 2 * * *"
    backup_database_url: str = ""
    backup_dir: str = ""
    backup_enabled: bool = False
    backup_keep_days: int = 14
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
    community_summary_system: str = _COMMUNITY_SUMMARY_SYSTEM_PROMPT
    consolidation_auto_merge_threshold: float = 0.9
    consolidation_borderline_floor: float = 0.75
    consolidation_prompt: str = _CONSOLIDATION_PROMPT
    context_token_budget: int = 2048
    contextual_bm25: bool = False
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
    eval_api_key: str = ""
    eval_concurrency: int = 4
    eval_judge: bool = False
    eval_judge_model: str = ""
    eval_max_tokens: int = 512
    eval_model: str = ""
    eval_sample_questions: int = 10
    eval_url: str = ""
    extract_custom_prompt: str = ""
    extract_max_tokens: int = 2048
    extract_min_chars: int = 80
    extract_preferences_prompt: str = _EXTRACT_PREFERENCES_PROMPT
    extract_strategy: str = "ontology"
    extract_summary_prompt: str = _EXTRACT_SUMMARY_PROMPT
    extract_system_prompt: str = _EXTRACT_SYSTEM_PROMPT
    extract_temperature: float = 0.0
    extract_timeout: float = 90.0
    fusion_depth: int = 50
    # Client-side cap on in-flight sidecar requests. Each sidecar is one torch process, so
    # a wide fan-out (graph build gates every chunk at once) queues here, not as timeouts.
    gliner_gate_concurrency: int = 8
    gliner_gate_floor: frozenset[str] = frozenset({"Person"})
    gliner_gate_threshold: float = 0.7
    gliner_gate_timeout: float = 30.0
    # The default gliner2 sidecar. The gate never loads weights, it only calls sidecars.
    gliner_gate_url: str = ""
    # Extra sidecars by variant name (gliner-relex and friends), each serving one checkpoint.
    gliner_gate_variants: dict[str, str] = {}
    graph_build_concurrency: int = 48
    graph_facts_k: int = 20
    identity_url: AnyHttpUrl = AnyHttpUrl("https://aizk.phvv.me")
    # VectorChord is the low-memory default. HNSW and tsvector are the portable fallback.
    index_backend: str = "vchordrq"
    insight_cron: str = "0 7 * * 0"
    insight_enabled: bool = True
    insight_facts_k: int = 40
    insight_max: int = 5
    insight_min_significance: float = 0.6
    insight_system: str = _INSIGHT_SYSTEM_PROMPT
    llm_api_key: str = Field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    llm_chat_template_kwargs: dict[str, bool] = {}
    llm_model: str = "gemma4-e2b-llm"
    llm_provider: str = "vllm"
    llm_request_timeout: float = 600.0
    llm_url: str = "http://localhost:8002/v1"
    log_level: str = "INFO"
    logto_url: AnyHttpUrl | None = None
    logto_client_id: str = ""
    logto_client_secret: SecretStr = SecretStr("")
    logto_management_resource: AnyHttpUrl = AnyHttpUrl("https://default.logto.app/api")
    logto_cache_seconds: PositiveFloat = 60.0
    logto_http_timeout: PositiveFloat = 10.0
    logto_required_scopes: frozenset[str] = frozenset({"control"})
    logto_writable_roles: frozenset[str] = frozenset({"admin", "editor"})
    louvain_seed: int = 7
    mcp_host: str = "127.0.0.1"
    mcp_public_url: AnyHttpUrl | None = None
    mcp_port: int = 8000
    ontology_match_threshold: float = 0.85
    ontology_prompt_template: str = _ONTOLOGY_PROMPT_TEMPLATE
    pg_client_launcher: list[str] = []
    multihop_max_hops: int = 2
    default_user_id: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")
    profiling: bool = False
    profile_on_write: bool = True
    profile_refresh_cron: str = "0 5 * * 0"
    profile_refresh_enabled: bool = True
    profile_system: str = _PROFILE_SYSTEM_PROMPT
    promoted_bonus: float = 0.01
    queue_batch_size: int = 64
    community_recall_k: int = 3
    fact_candidate_factor: int = 2
    graph_dangling_factor: float = 0.5
    graph_entity_seed_weight: float = 1.0
    # Whether query entity mentions seed the graph expansion at all, the R2 ablation's
    # off switch; off also skips the gate's extract call on every recall.
    graph_entity_seeding: bool = True
    graph_fact_seed_weight: float = 0.25
    graph_mass_window: int = 80
    graph_mention_fuzzy: bool = True
    graph_mention_mass: float = 10.0
    graph_ppr_damping: float = 0.5
    graph_ppr_frontier: int = 32
    graph_seed_entities: int = 16
    profile_recall_k: int = 1
    recall_chars_per_token: float = 4.0
    recall_frequency_weight: float = 0.02
    # Calibrated on real Qwen3-VL query/document embeddings: relevant chunks land at cosine
    # distance 0.27-0.49 while off-corpus questions bottom out at 0.60-0.75.
    recall_max_distance: float = 0.65
    recall_per_document: int = 3
    recall_recency_half_life_days: float = 30.0
    recall_recency_weight: float = 0.1
    rerank_api_key: str = ""
    rerank_depth: int = 50
    rerank_instruction: str = (
        "Given a question about stored memory, judge whether the evidence answers it."
    )
    rerank_model: str = "qwen3-reranker"
    # The official Qwen3-Reranker scaffold. Serving the original checkpoint as a yes/no
    # classifier leaves score calibration to this exact prompt shape; without it the scores
    # are noise. Empty templates send the raw texts for models that need none.
    rerank_query_template: str = (
        "<|im_start|>system\nJudge whether the Document meets the requirements based on the"
        ' Query and the Instruct provided. Note that the answer can only be "yes" or'
        ' "no".<|im_end|>\n<|im_start|>user\n<Instruct>: {instruction}\n<Query>: {query}\n'
    )
    rerank_document_template: str = (
        "<Document>: {document}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )
    rerank_request_timeout: float = 30.0
    rerank_url: str = "http://localhost:8004/v1"
    raptor_cron: str = "30 4 * * 0"
    raptor_enabled: bool = True
    raptor_every_n_facts: int = 50
    raptor_k: int = 3
    raptor_max_levels: int = 5
    raptor_redundancy_threshold: float = 0.95
    raptor_rollup_system: str = _RAPTOR_ROLLUP_SYSTEM_PROMPT
    raptor_root_max: int = 3
    raptor_sim_threshold: float = 0.5
    reembed_batch: int = 128
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

    @model_validator(mode="after")
    def default_dsns(self) -> Self:
        """Fill an unset `database_url`/`admin_database_url` from the host/port/db and
        passwords."""
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

    @model_validator(mode="after")
    def complete_auth(self) -> Self:
        """Reject a partial Logto configuration while preserving local auth-off mode."""
        if self.logto_url is None:
            return self
        missing = [
            name
            for name, value in (
                ("mcp_public_url", self.mcp_public_url),
                ("logto_client_id", self.logto_client_id),
                (
                    "logto_client_secret",
                    self.logto_client_secret.get_secret_value(),
                ),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Logto authentication requires {', '.join(missing)}")
        return self

    @property
    def asyncpg_dsn(self) -> str:
        """The app-role `database_url` with the `+asyncpg` driver tag dropped."""
        return self.database_url.replace("+asyncpg", "", 1)

    @property
    def admin_asyncpg_dsn(self) -> str:
        """The owner-role `admin_database_url` with the `+asyncpg` driver tag dropped."""
        return self.admin_database_url.replace("+asyncpg", "", 1)

    @property
    def app_role(self) -> str:
        """Name of the restricted role the app connects under, read from `database_url`."""
        return str(urlsplit(self.database_url).username)

    @property
    def mcp_resource_id(self) -> str:
        """The RFC 8707 resource indicator this server is, the `aud` a valid token must
        carry."""
        if self.mcp_public_url is None:
            return ""
        return f"{str(self.mcp_public_url).rstrip('/')}/mcp"

    def subject_id(self, subject: str) -> UUID5:
        """Derive a stable Aizk user ID from an external subject."""
        namespace = str(self.identity_url).rstrip("/")
        return uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}/subjects/{subject}")

    def scope_id(self, external_id: str) -> UUID5:
        """Derive a stable Aizk scope ID from an external or synthetic identifier."""
        namespace = str(self.identity_url).rstrip("/")
        return uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}/scopes/{external_id}")

    def scope_ids(self, external_ids: str | None) -> frozenset[uuid.UUID]:
        """Derive scope IDs from a comma-separated operator input."""
        names = (name.strip() for name in (external_ids or "").split(","))
        return frozenset(self.scope_id(name) for name in names if name)

    @property
    def chunk_denylist_languages(self) -> frozenset[str]:
        """The `chunk_denylist` comma-separated field, parsed into an immutable language set."""
        return frozenset(self.chunk_denylist.split(","))

    def for_statement(self, statement: Select) -> dict[str, object]:
        """The settings values a statement's required binds name, ready to execute with.

        Tunable binds carry their settings field names, so the statement itself selects
        which values travel and a changed setting takes effect on the very next call.
        """
        return self.model_dump(include=set(statement_binds(statement)))


@cache
def statement_binds(statement: Select) -> frozenset[str]:
    """The named binds a statement requires at execution, read off its own compilation."""
    return frozenset(name for name, bind in statement.compile().binds.items() if bind.required)
