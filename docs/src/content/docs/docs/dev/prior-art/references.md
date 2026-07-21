---
title: "References and lineage"
description: "Which mechanism came from which paper, and which are ours."
---

aizk is a mix of published memory mechanisms, open source infrastructure, product comparisons, and
work designed here. This page records which is which, so a reader can trace any behavior back to
either a paper or a deliberate choice. A citation on this page does not mean aizk copied an
implementation, and it does not mean the cited project endorses aizk. Every code path named here
was checked against the tree.

## How to read the map

```text
  published work ──adopted, follows the design closely──▶ shipped mechanism
  published work ──adapted, idea kept, shape changed────▶ shipped mechanism
  published work ┄┄compared, no code┄┄▶ product boundary
  published work ┄┄workflow, no runtime┄┄▶ how we change the code
  designed for aizk ──original──▶ shipped mechanism
```

| Label | Meaning |
|---|---|
| adopted | the shipped mechanism follows the cited design closely |
| adapted | the source supplied the idea and aizk changed its shape |
| compared | the source helped define a product boundary but supplied no implementation |
| workflow | the source influenced how the code is changed or checked |
| original | the mechanism was designed for aizk and is not claimed from the cited systems |

## Memory and retrieval

| Feature | Lineage | What aizk does | Code |
|---|---|---|---|
| temporal entity and fact graph | adopted from [Zep and Graphiti](https://arxiv.org/abs/2501.13956) | immutable content plus valid-time and recorded-time claims | `store/models/tables/`, `store/models/views/live_fact.py` |
| add, update, no-op consolidation | adapted from [Mem0](https://arxiv.org/abs/2504.19413) | rules settle the confident cases and only ambiguity reaches the LLM | `graph/consolidation.py`, `graph/writer.py` |
| associative multi-hop recall | adapted from [HippoRAG 2](https://arxiv.org/abs/2502.14802) | personalized PageRank inside the SQL statement, over visible current facts only | `retrieval/lanes/facts.py` |
| community summaries | adapted from [GraphRAG](https://arxiv.org/abs/2404.16130) and [LightRAG](https://arxiv.org/abs/2410.05779) | communities as a rebuildable global-evidence projection | `graph/communities.py`, `retrieval/lanes/vector.py` |
| recursive summary tree | adopted from [RAPTOR](https://arxiv.org/abs/2401.18059) | grounded summaries rolled into bounded higher levels | `graph/raptor.py`, `retrieval/lanes/overview.py` |
| reflective observations | adapted from [A-MEM](https://arxiv.org/abs/2502.12110) | optional observations that never replace their grounding facts | `graph/insight.py` |
| entity profiles | adapted from [GAM](https://arxiv.org/abs/2604.12285) | one evidence-grounded profile per entity and scope set | `graph/profiles.py` |
| raw evidence as authority | supported by [Does Memory Need Graphs](https://arxiv.org/abs/2601.01280) | source chunks stay primary and each graph lane earns its cost in ablation | `retrieval/lanes/sources.py`, `eval/plans.py` |
| append-only corrective history | supported by [APEX-MEM](https://arxiv.org/abs/2604.14362) | contradicted knowledge has its range closed rather than being deleted | `store/models/tables/fact.py` |
| speaker-aware group memory | adapted from [GroupMemBench](https://arxiv.org/abs/2605.14498) and [Hindsight](https://aclanthology.org/2026.acl-demo.27/) | objective state kept apart from observations, opinions, experiences, preferences | `provenance.py`, `graph/grounding.py`, `eval/groupmem.py` |
| forgetting-aware evaluation | adopted from [Memora](https://arxiv.org/abs/2604.20006) | scores current evidence without rewarding expired memory | `eval/metrics.py` |
| workflow and premise categories | planned from [LongMemEval-V2](https://arxiv.org/abs/2605.12493) | kept in evaluation until a production schema earns them | `eval/` |
| action-memory boundary | compared with [Mem2ActBench](https://aclanthology.org/2026.acl-long.370/) | no action-selection claim is made from a retrieval-only score | [External benchmarks](/docs/dev/eval/external/) |
| dense and lexical fusion | adapted from [Reciprocal Rank Fusion](https://research.google/pubs/reciprocal-rank-fusion-outperforms-condorcet-and-individual-rank-learning-methods/) | typed lane ranks fused inside one SQL recall program | `retrieval/recall/program.py`, `retrieval/lanes/sources.py` |
| merit ordering and maximal recall | original | every lane stays available and one cross-encoder ranks the candidates together | `retrieval/recall/orchestrator.py`, `retrieval/rerank/rescore.py` |
| public evidence provenance | original | internal lanes collapse into source, derived, and session evidence with exact scope descriptions | `retrieval/models/result.py`, `retrieval/templates/recall.md.j2` |

Paths in that table are relative to `src/`. RAPTOR supports hierarchical summaries, GraphRAG
supports community summaries, HippoRAG supports associative graph retrieval, and GAM and A-MEM
support consolidated representations. None of them argues that the agent on the other side of the
API should ever see a lane name. The three public provenance classes are therefore an interface
choice made here, based on what a consumer needs in order to judge evidence rather than on how the
engine happened to find it.

## Sharing and identity

The split between private and shared memory is informed by
[Collaborative Memory](https://arxiv.org/abs/2505.18279). aizk turns that paper's policy graph into
one PostgreSQL-native scope lattice, where every row carries a sorted nonempty set of scope UUIDs
and a reader has to stand in every member.
[Scope sets in depth](/docs/dev/identity/scope-sets/) has the mechanics.

The intersection model, full-authority reads with one explicit write destination, and the
source-preserving `share` operation are original. Logto stays authoritative for users,
organizations, roles, and public organization metadata, and aizk derives stable IDs from verified
token claims without storing an identity or membership mirror at all.

| Concern | Source | The aizk boundary |
|---|---|---|
| identity and organization authority | [Logto](https://logto.io/) | OIDC discovery, signed tokens, current org roles, no local identity tables |
| OAuth protected MCP | [FastMCP](https://github.com/jlowin/fastmcp) | dynamic client registration and an OIDC proxy over persistent encrypted state |
| database authorization | [PostgreSQL row security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) and the house `rlsalchemy` package | forced policies on both content and scoped claims |
| multi-user memory model | [Collaborative Memory](https://arxiv.org/abs/2505.18279) | private, organization, and intersection scopes with immutable capture provenance |

## Infrastructure and model lanes

| Responsibility | Project | Use in aizk |
|---|---|---|
| relational, temporal, lexical, vector, and policy execution | [PostgreSQL](https://www.postgresql.org/) | one durable state engine and the preferred place for filtering, ranking, hashing, and temporal logic |
| vector index | [VectorChord](https://github.com/tensorchord/VectorChord) and [pgvector](https://github.com/pgvector/pgvector) | low-memory production vector search with a portable fallback |
| ORM and validation | [SQLModel](https://sqlmodel.tiangolo.com/), [SQLAlchemy](https://www.sqlalchemy.org/), [Pydantic](https://docs.pydantic.dev/) | typed models, PostgreSQL statements, and wire contracts |
| durable jobs | [PgQueuer](https://github.com/JanBjorge/PgQueuer) | graph projection and scheduled passes without a bespoke workflow ledger |
| document conversion | [Docling](https://github.com/docling-project/docling) and [Docling Serve](https://github.com/docling-project/docling-serve) | private conversion of accepted bytes into structured JSON and normalized Markdown |
| immutable object bytes | [SeaweedFS](https://github.com/seaweedfs/seaweedfs) and [obstore](https://github.com/developmentseed/obstore) | private S3-compatible storage behind opaque keys |
| malware scanning | [ClamAV](https://docs.clamav.net/manual/Usage/ClamdProtocol.html) | fail-closed streaming scan before any object is persisted |
| log collection, storage, inspection | [Grafana Alloy](https://grafana.com/docs/alloy/latest/), [Loki](https://grafana.com/docs/loki/latest/), [Grafana](https://grafana.com/docs/grafana/latest/) | labeled Docker logs, one bounded store, a loopback-only viewer |
| log event vocabulary | [OpenTelemetry Logs Data Model](https://opentelemetry.io/docs/specs/otel/logs/data-model/) | structured events while PostgreSQL stays the durable usage authority |
| MCP transport and OAuth | [FastMCP](https://github.com/jlowin/fastmcp) | the public tools and the Logto OIDC proxy |
| browser application | [SvelteKit](https://svelte.dev/docs/kit) and [@logto/sveltekit](https://docs.logto.io/quick-starts/sveltekit) | the optional web interface over the browser JSON API |
| model serving | [vLLM](https://github.com/vllm-project/vllm) with [structured outputs](https://docs.vllm.ai/en/latest/features/structured_outputs/) | replaceable OpenAI-compatible endpoints with grammar-constrained extraction |
| typed LLM calls and judging | [Pydantic AI](https://ai.pydantic.dev/) and [Pydantic Evals](https://ai.pydantic.dev/evals/) | schema-constrained extraction and isolated evaluation |
| chunking | [Chonkie](https://github.com/chonkie-inc/chonkie) | bounded prose and source windows |
| fast entity gate | [GLiNER2](https://github.com/fastino-ai/GLiNER2) | a cheap GPU gate and an experimental extractor, never the production graph authority |
| production embedding | [Qwen3-VL-Embedding-2B](https://huggingface.co/Qwen/Qwen3-VL-Embedding-2B) | text and image vectors through a generic client |
| production reranking | [Qwen3-Reranker-4B](https://huggingface.co/Qwen/Qwen3-Reranker-4B) | cross-encoder merit ordering across every lane |
| production extraction | [Gemma 4 12B](https://huggingface.co/google/gemma-4-12B-it-qat-w4a16-ct) | grounded graph extraction through the generic `LLM` client |
| profiling, environments, remote runs | the house `mainboard`, `chefe`, and `lote` packages | stage timing, reproducible tasks, and deployment |
| typed patterns and SQL primitives | the house `patos` package | shared model, registry, and `patos.sql` column abstractions |

Model names are deployment choices and not domain names in the code. Embedding, reranking, gating,
and extraction each sit behind a client, so another compatible provider can take over without any
part of the memory engine being renamed.

## Original files and derived data

Several mature systems separate an authoritative original from replaceable interpretation, which is
exactly the shape the artifact path takes.

| Reference | Useful mechanism | The aizk adaptation |
|---|---|---|
| [Paperless-ngx](https://docs.paperless-ngx.com/usage/) | preserve the original, track checksums, index the derivative | one original blob stays authoritative while Markdown and structured data live in PostgreSQL |
| [Docling formats](https://docling-project.github.io/docling/usage/supported_formats/) | emit normalized Markdown plus a lossless structured document | both derivatives are stored against the exact original revision |
| [Unstructured elements](https://docs.unstructured.io/concepts/document-elements) | normalize many formats into typed elements with source metadata | source metadata is retained without adopting a second element store |
| [ColPali](https://arxiv.org/abs/2407.01449) | retrieve pages visually rather than through lossy text | one supplemental image vector beside authoritative Docling structure |
| [VisRAG](https://arxiv.org/abs/2410.10594) | answer from page images | authorized files stay available on demand, and recall transfers no bytes |
| [M3DocRAG](https://arxiv.org/abs/2411.04952) | combine visual and textual evidence | page-level and video retrieval stay deferred until they measure better |

## Knowledge organization

| Source | Inherited idea | The aizk adaptation |
|---|---|---|
| [The PARA Method](https://fortelabs.com/blog/para/) | Projects are finite outcomes and Areas are ongoing responsibilities | Areas and Projects are ontology entities rather than folders |
| [Second Brain and Zettelkasten](https://zettelkasten.de/posts/building-a-second-brain-and-zettelkasten/) | PARA gives action context while a Zettelkasten gives atomic durable knowledge | one maintained brief per Area or Project, with atomic notes tagged into it |
| the author's own Zettelkasten structure notes | `#project` and `#area` identify structure notes | key-value source tags name an exact entity of any live ontology kind and imply no status or access |

## What is original to aizk

Cite the following as design done here rather than attributing it to one upstream paper.

- Arbitrary nonempty scope sets with intersection visibility under forced PostgreSQL RLS.
- Content-addressed graph content held separate from scoped bi-temporal claims.
- Full-authority recall paired with one explicit write destination.
- A source-preserving `share` that creates provenance-linked copies rather than moving a row.
- One maximal recall plan whose cross-encoder orders every lane by merit.
- A single prompt-ready MCP recall string produced by a token-budget prefix.
- Exact artifact revision resources that stay authorized by PostgreSQL and transfer no bytes during
  recall.
- An original-only blob model with database derivatives, metadata fallback, adaptive compression,
  shared physical bytes, a fail-closed scan gate, and no Redis anywhere.
- Durable actor and scope usage accounting kept apart from expiring operational logs.
- A health snapshot that checks schema, policy, jobs, models, scopes, graph freshness, and a real
  recall in under five seconds.

## Workflow influences

The [Bun Rust rewrite report](https://bun.com/blog/bun-in-rust) and its original
[`PORTING.md` commit](https://github.com/oven-sh/bun/commit/46d3bc29f270fa881dd5730ef1549e88407701a5)
shaped how large refactors are run here and not the runtime. The reusable parts are a written
mapping before a broad change, small trial cells, bounded ownership, an independent adversarial
audit, errors treated as a work queue, and a test suite as the final authority. No Bun code is
copied, and compiling is never taken as proof of behavior.

## Next

<div class="not-content">

- [Comparison](/docs/dev/prior-art/comparison/) puts these mechanisms beside other systems.
- [Rejected and deferred](/docs/dev/prior-art/rejected/) covers the sources whose ideas were not taken.
- [Design principles](/docs/dev/architecture/principles/) explains the rules the original work follows.

</div>
