# References

Aizk combines published memory mechanisms, open source infrastructure, product comparisons, and
original work. This page records which is which. A citation here does not mean that Aizk copied an
implementation or that another project endorses Aizk.

## How to read the map

| Label | Meaning |
|---|---|
| adopted | the shipped mechanism follows the cited design closely |
| adapted | the source supplied the idea and Aizk changed its shape |
| compared | the source helped define product boundaries but supplied no implementation |
| workflow | the source influenced how the code was changed or checked |
| original | the mechanism was designed for Aizk and is not claimed from the cited systems |

## Memory and retrieval features

| Aizk feature | Lineage | What Aizk does | Code |
|---|---|---|---|
| temporal entity and fact graph | adopted from [Zep and Graphiti](https://arxiv.org/abs/2501.13956) | stores immutable content plus valid-time and recorded-time claims | `store/models/tables`, `store/models/views/live_fact.py` |
| add, update, and no-op consolidation | adapted from [Mem0](https://arxiv.org/abs/2504.19413) | resolves confident cases by rule and sends only ambiguity to the LLM | `graph/consolidation.py`, `graph/writer.py` |
| associative multi-hop recall | adapted from [HippoRAG 2](https://arxiv.org/abs/2502.14802) | runs personalized PageRank over only visible current facts | `retrieval/lanes/graph.py` |
| community summaries | adapted from [GraphRAG](https://arxiv.org/abs/2404.16130) and [LightRAG](https://arxiv.org/abs/2410.05779) | detects communities as a rebuildable global-evidence projection | `graph/communities.py`, `retrieval/lanes/vector.py` |
| recursive summary tree | adopted from [RAPTOR](https://arxiv.org/abs/2401.18059) | rolls grounded summaries into bounded higher levels | `graph/raptor.py`, `retrieval/lanes/overview.py` |
| reflective observations | adapted from [A-MEM](https://arxiv.org/abs/2502.12110) | derives optional observations without replacing their grounding facts | `graph/insight.py` |
| entity profiles | adapted from [GAM](https://arxiv.org/abs/2604.12285) | builds one evidence-grounded profile per entity and scope set | `graph/profiles.py` |
| raw evidence as authority | supported by [Does Memory Need Graphs](https://arxiv.org/abs/2601.01280) | keeps source chunks primary and makes every graph lane earn its cost in ablation | `retrieval/lanes/sources.py`, `eval/plans.py` |
| append-only corrective history | supported by [APEX-MEM](https://arxiv.org/abs/2604.14362) | closes temporal ranges rather than deleting contradicted knowledge | `store/models/tables/fact.py` |
| speaker-aware group memory | adapted from [GroupMemBench](https://arxiv.org/abs/2605.14498) and [Hindsight](https://aclanthology.org/2026.acl-demo.27/) | separates objective state from observations, opinions, experiences, and preferences | `provenance.py`, `graph/grounding.py`, `eval/groupmem.py` |
| forgetting-aware evaluation | adopted from [Memora](https://arxiv.org/abs/2604.20006) | measures current evidence without rewarding expired memory | `eval/metrics.py` |
| workflow and premise benchmark categories | planned from [LongMemEval-V2](https://arxiv.org/abs/2605.12493) | keeps these categories in evaluation until a production schema earns them | `eval` |
| action-memory boundary | compared with [Mem2ActBench](https://aclanthology.org/2026.acl-long.370/) | makes no action-selection claim from a retrieval-only score | `docs/benchmarks.md` |
| dense and lexical fusion | adapted from [Reciprocal Rank Fusion](https://research.google/pubs/reciprocal-rank-fusion-outperforms-condorcet-and-individual-rank-learning-methods/) | fuses typed source ranks inside one SQL recall program | `retrieval/recall/program.py`, `retrieval/lanes/sources.py` |
| merit ordering and maximal recall | original measured Aizk result | keeps every lane available and lets one cross-encoder rank candidates together | `retrieval/recall/orchestrator.py`, `retrieval/rerank/rescore.py` |
| public evidence provenance | original Aizk interface design | collapses internal lanes into source, derived, and session evidence and attaches exact Logto scope descriptions | `retrieval/models/result.py`, `retrieval/templates/recall.md.j2` |

RAPTOR supports hierarchical summaries at several abstraction levels. GraphRAG supports community
summaries for broad corpus questions. HippoRAG supports graph-based associative retrieval. GAM and
A-MEM support consolidated and interconnected memory representations. These papers justify trying
different internal representations, but none argues that an agent should receive Aizk's lane names.
The three public provenance classes are therefore an original interface choice based on what the
consumer needs to judge evidence rather than how the engine found it.

## Sharing, identity, and security

The private and shared memory distinction is informed by
[Collaborative Memory](https://arxiv.org/abs/2505.18279). Aizk changes the paper's policy graph into
one PostgreSQL-native scope lattice. Every row carries a sorted nonempty set of scope UUIDs and a
reader must stand in every member. Forced row level security applies that rule to documents,
chunks, graph claims, profiles, communities, and working memory.

The intersection model, full-authority reads, explicit write destinations, and source-preserving
`share` operation are original Aizk design. Logto remains authoritative for users, organizations,
roles, and public organization metadata. Aizk derives stable IDs from verified claims and stores no
identity or membership mirror.

| Concern | Source | Aizk boundary |
|---|---|---|
| identity and organization authority | [Logto](https://logto.io/) | OIDC discovery, signed tokens, current organization roles, and no local identity tables |
| OAuth protected MCP | [FastMCP](https://github.com/jlowin/fastmcp) | dynamic client registration and an OIDC proxy backed by persistent encrypted state |
| database authorization | [PostgreSQL row security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) and the house `rlsalchemy` package | forced policies on both content and scoped claims |
| multi-user memory model | [Collaborative Memory](https://arxiv.org/abs/2505.18279) | private, organization, and intersection scopes with immutable capture provenance |

## Infrastructure and model lanes

| Responsibility | Project | Use in Aizk |
|---|---|---|
| relational, temporal, lexical, vector, and policy execution | [PostgreSQL](https://www.postgresql.org/) | one durable state engine and the preferred place for filtering, ranking, hashing, comparison, and temporal logic |
| vector index | [VectorChord](https://github.com/tensorchord/VectorChord) and [pgvector](https://github.com/pgvector/pgvector) | low-memory production vector search with a portable fallback |
| ORM and validation | [SQLModel](https://sqlmodel.tiangolo.com/), [SQLAlchemy](https://www.sqlalchemy.org/), and [Pydantic](https://docs.pydantic.dev/) | typed models, PostgreSQL statements, and wire contracts |
| durable jobs | [PgQueuer](https://github.com/JanBjorge/PgQueuer) | graph projection and scheduled maintenance without a bespoke workflow ledger |
| MCP transport and OAuth | [FastMCP](https://github.com/jlowin/fastmcp) | the four public tools and the Logto OIDC proxy |
| model serving | [vLLM](https://github.com/vllm-project/vllm) | replaceable OpenAI-compatible embedding, reranking, and extraction endpoints |
| typed LLM calls and judging | [Pydantic AI](https://ai.pydantic.dev/) and [Pydantic Evals](https://ai.pydantic.dev/evals/) | schema-constrained extraction and isolated evaluation |
| chunking | [Chonkie](https://github.com/chonkie-inc/chonkie) | bounded prose and source windows |
| fast entity gate | [GLiNER2](https://github.com/fastino-ai/GLiNER2) and [GLiNER2 large](https://huggingface.co/fastino/gliner2-large-v1) | a cheap GPU gate and an experimental extractor, not the production graph authority |
| production embedding | [Qwen3-VL-Embedding-2B](https://huggingface.co/Qwen/Qwen3-VL-Embedding-2B) | text and image vectors through a generic client |
| production reranking | [Qwen3-Reranker-4B](https://huggingface.co/Qwen/Qwen3-Reranker-4B) | cross-encoder merit ordering across all retrieval lanes |
| production extraction | [Gemma 4 31B](https://huggingface.co/google/gemma-4-31B-it-qat-w4a16-ct) | grounded graph extraction through the generic `LLM` client |
| profiling | the house `mainboard` package | stage timing, memory sizing, and production bottleneck evidence |
| environments and remote operation | the house `chefe` and `lote` packages | reproducible tasks and Crimson deployment |
| typed patterns | the house `patos` package | shared model and registry abstractions where they reduce code |

Model names are deployment choices rather than domain names in the code. Embedding, reranking,
gating, and extraction clients can move to another compatible provider without renaming the memory
engine.

## Product and project comparisons

These systems clarified the product surface but did not supply Aizk code.

| Reference | What we learned | Where Aizk deliberately differs |
|---|---|---|
| [Supermemory](https://supermemory.ai/docs) | excellent separation of getting started, concepts, operations, integrations, and benchmarks plus a clear document and memory mental model | Aizk exposes its engine source, makes raw evidence authoritative, and represents overlapping scope intersections under forced PostgreSQL RLS |
| [Supermemory repository](https://github.com/supermemoryai/supermemory) | a small client surface and MCP-first onboarding are valuable | Aizk keeps the database, extraction, ranking, temporal graph, and authorization implementation in the auditable repository |
| [Tanka](https://www.tanka.ai/) | shared memory should preserve high-density human intent from a large stream of automated execution | Aizk is memory infrastructure rather than a messenger or company operating system |
| [EverOS](https://github.com/EverMind-AI/EverOS) | Tanka's open framework separates atomic facts, episodes, profiles, and agentic use | Aizk does not adopt its proactive work layer or make profiles authoritative |
| [SharedMemory](https://docs.sharedmemory.ai/) | a short write and query API, isolated volumes, conflict checks, and multi-agent access are useful product language | Aizk uses three MCP verbs, database RLS, bi-temporal claims, and source-first retrieval rather than adopting its guard or volume model |
| [Brainshared](https://brainshared.com/) | historical inspiration for turning the best validated individual memory into a shared team floor | the site is no longer an available implementation reference and Aizk does not depend on it |
| [Cognee](https://github.com/topoteretes/cognee) | a PostgreSQL node and edge schema can unify graph and vector storage | Aizk was written from scratch as a smaller PostgreSQL-only engine with its own scope lattice |
| [Mem0](https://github.com/mem0ai/mem0) and [Letta](https://github.com/letta-ai/letta) | the useful boundary sits between a minimal memory API and a full agent framework | Aizk is memory infrastructure and does not own the agent loop |
| the authored Zettelkasten and `qmd` | exact files remain the human source of truth and the honest retrieval baseline | Aizk supplies scoped current-state recall, temporal history, and one prompt-ready result rather than replacing authored notes |

## Knowledge organization

| Source | Inherited idea | AIZK adaptation |
|---|---|---|
| [The PARA Method](https://fortelabs.com/blog/para/) | Projects are finite action-oriented outcomes while Areas are ongoing responsibilities that need attention | Areas and Projects remain ontology entities rather than folders, and source tags associate knowledge without moving it |
| [Combining Building a Second Brain and the Zettelkasten Method](https://zettelkasten.de/posts/building-a-second-brain-and-zettelkasten/) | PARA supplies action context while a Zettelkasten supplies atomic durable knowledge and structure notes that link supporting material | one maintained brief maps each Area or Project, while atomic supporting notes use `#project: <name>` and `#area: <name>` |
| the user's historical Zettelkasten Structure Note | `#project` and `#area` identify structure notes and every Project belongs to an Area | key-value source tags name the exact entity, work for any live ontology kind, and never imply status or access |

“Shared brain” is Aizk's product description. The phrase also acknowledges the historical
Brainshared product idea, but no Brainshared package or code runs inside Aizk. Here it means that
several people and MCP agents can use one memory service while database policy preserves private,
shared, and intersection knowledge.

## Engineering workflow references

The [Bun Rust rewrite report](https://bun.com/blog/bun-in-rust) and its original
[`PORTING.md` commit](https://github.com/oven-sh/bun/commit/46d3bc29f270fa881dd5730ef1549e88407701a5)
influenced the refactor process rather than the runtime. The reusable ideas are a written mapping
before a broad change, small trial cells, bounded ownership, independent adversarial audit, errors
as a work queue, and a language-independent test suite as the final authority. Aizk does not copy
Bun code and does not treat compilation as proof of behavior.

The documentation information architecture takes inspiration from Supermemory's current docs. The
text, examples, diagrams, visual system, and claims here are original to Aizk.

## Rejected and deferred ideas

References also matter when the evidence says not to ship a mechanism.

| Idea | Status | Reason |
|---|---|---|
| query-time routing | rejected | the measured classifier was only 44 percent accurate and a wrong route could remove decisive evidence |
| fixed lane ordering | rejected | facts-first and overview-first each failed on a different evaluation stratum |
| GLiNER2 as graph authority | rejected for production | the large GPU model is fast but still misses dense relations that the LLM extracts |
| graph-only authority | rejected | source briefs and chunks are more faithful than generated profiles and summaries |
| human acceptance queue | permanently rejected | AIZK has no acceptance layer because agents write sources directly and correct them when evidence changes |
| bespoke graph workflow ledger | rejected for now | the graph is a rebuildable projection and PgQueuer already owns durable execution state |
| TRACE-KG ontology induction | studied, not adopted | Aizk currently prefers a declared ontology with deterministic validation |
| Tanka-style proactive work layer | deferred | Aizk supplies memory and does not own the agent's planning or action loop |
| Supermemory-style connector breadth | deferred | current work prioritizes correctness, access, time, and evaluation over ingestion breadth |

## Original Aizk contributions

The following pieces should be cited as Aizk design rather than attributed to one upstream paper.

- arbitrary nonempty scope sets with intersection visibility under forced PostgreSQL RLS
- content-addressed graph content separated from scoped bi-temporal claims
- full-authority recall with one explicit write destination
- a source-preserving `share` operation that creates provenance-linked copies
- one maximal recall plan whose cross-encoder orders every lane by merit
- a single prompt-ready MCP recall string produced by a token-budget prefix
- an operational health snapshot that checks schema, policy, jobs, models, scopes, graph freshness,
  and a real recall in under five seconds

The [Benchmarks](benchmarks.md) page records what has been measured. The [Comparison](comparison.md)
page separates observed results from architectural differences. Neither page turns a related
paper, model, or product into an endorsement.
