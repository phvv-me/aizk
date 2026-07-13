# Provenance

Aizk combines published memory mechanisms with an original authorization model. Implemented code
and research hypotheses stay separate.

## Implemented foundations

| Mechanism | Source | Aizk implementation |
|---|---|---|
| immutable graph content and scoped temporal claims | [Zep and Graphiti](https://arxiv.org/abs/2501.13956) | `store/models/tables` |
| add, update, and duplicate consolidation | [Mem0](https://arxiv.org/abs/2504.19413) | `graph/consolidation.py`, `graph/writer.py` |
| personalized PageRank | [HippoRAG 2](https://arxiv.org/abs/2502.14802) | `graph/algos.py`, `retrieval/recall.py` |
| community summaries | [GraphRAG](https://arxiv.org/abs/2404.16130) and [LightRAG](https://arxiv.org/abs/2410.05779) | `graph/communities.py` |
| recursive summary tree | [RAPTOR](https://arxiv.org/abs/2401.18059) | `graph/raptor.py` |
| reflective observations | [A-MEM](https://arxiv.org/abs/2502.12110) | `graph/insight.py` |
| entity profiles | [GAM](https://arxiv.org/abs/2604.12285) | `graph/profiles.py` |
| speaker-aware group evaluation | [GroupMemBench](https://arxiv.org/abs/2605.14498) | `eval` |
| forgetting-aware accuracy | [Memora](https://arxiv.org/abs/2604.20006) | `eval/metrics.py` |
| typed dense, lexical, fact, and neighbor fusion | original Aizk design | `retrieval/query/hybrid.py` |
| overlapping authorization scopes | original Aizk design | `store/mixins/scoped.py` |

## Collaboration model

Logto remains authoritative for users, organizations, roles, and public organization metadata.
Aizk derives stable UUIDs from verified subject and organization claims. It stores no identity or
membership mirror.

Every authorized row carries a sorted nonempty scope set. A personal UUID is private memory. One
organization UUID is ordinary team memory. A larger set is an intersection visible only to a
caller standing in every member. Forced RLS enforces the lattice in PostgreSQL.

Scope answers who may access a row. Capture context answers who said or experienced something.
The author label and role are immutable display snapshots. Objective world state shares one
consolidation partition. Experience, observation, opinion, and preference use one partition per
creator. This distinction follows the failure modes in GroupMemBench and the epistemic categories
demonstrated by [Hindsight](https://aclanthology.org/2026.acl-demo.27/).

## Current research boundary

[Does Memory Need Graphs](https://aclanthology.org/2026.acl-long.1232/) finds that raw sessions and
independent structured keys form a strong baseline. Similarity edges can add noise, and summaries
can crowd out evidence. Aizk therefore treats PageRank, communities, RAPTOR, profiles, reranking,
and context order as measurable lanes rather than automatic improvements.

[LongMemEval-V2](https://arxiv.org/abs/2605.12493) separates static state, dynamic state,
workflows, gotchas, and premise awareness. Those categories should become benchmark records before
they become new production schema.

[APEX-MEM](https://aclanthology.org/2026.acl-long.749/) supports append-only temporal history and
retrieval-time conflict handling. Aizk now keeps backdated updates as history and preserves
speaker-bound conflicts. Confidence and evidence-weighted conflict resolution remain future work.

[Mem2ActBench](https://aclanthology.org/2026.acl-long.370/) measures whether memory changes tool
selection and arguments. Aizk currently evaluates retrieval and grounded answers. It does not yet
claim action-memory performance.

## Evaluation boundary

The internal harness measures visible memory already stored in Aizk. GroupMemBench is different.
Its adapter imports each released message with speaker and source time into an isolated shared
scope, builds the graph, recalls as the named asker, and judges the generated answer. A benchmark
name is never attached to a score made from isolated questions or an ambient corpus.
