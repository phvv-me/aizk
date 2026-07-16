# Comparison

## Against vault search

Literal search remains the right tool for exact strings and file discovery. Aizk is for questions
that need meaning, source time, speaker perspective, shared scopes, or evidence across documents.
It should complement `rg` and qmd rather than replace them.

| Need | Vault tools | Aizk |
|---|---|---|
| exact term or path | fastest and simplest | unnecessary overhead |
| paraphrased intent | embedding search where available | dense and lexical fusion |
| shared project memory | no authorization model | Logto-derived scope lattice |
| overlap of organizations | manual duplication | native scope intersection |
| speaker belief or preference | prose interpretation | captured and attributed perspective |
| point-in-time fact replay | Git archaeology | bi-temporal range query |
| sourced agent context | manual note assembly | budgeted MCP context pack |

### Managed knowledge comparison on 2026-07-15

The comparison refreshed the full QMD index after the management notes were normalized, then asked
both systems the same eight representative questions. Aizk ranked the intended current brief first
for eight of eight questions. QMD without reranking did so for five of eight. This was a manual
first-source check rather than an answer-generation benchmark.

| Question family | Aizk first | QMD first |
|---|---:|---:|
| current open Projects | yes | yes |
| current Aizk state | yes | yes |
| current Japanese Area state | yes | no |
| JLPT N2 Window Weekly Plan | yes | yes |
| Personal Brand and Career Website | yes | yes |
| whether graph memory improves answers | yes | no |
| evaluating obsolete memory | yes | yes |
| next action for My Personal Computer | yes | no |

QMD remained excellent when the question named one exact Project or durable note. Its failures were
broad current-state questions where a related journal or Area note outranked the authority, and one
conceptual paper question where a CAGRA graph-index note outranked the memory-research note. Aizk's
explicit managed-document identity, status-aware database catalogs, civil source dates, and
maximal-title authority made those cases reliable without a query router.

The stable QMD path used `--no-gpu --no-rerank`. Its observed latency ranged from about 1.2 to
29.6 seconds and varied heavily on cold requests. CUDA initialization repeatedly failed because
CMake could not resolve `CUDA::cublas`, while reranking sometimes stopped after announcing the
rerank stage without returning results. Sequential Aizk recalls over the same representative set
took about 0.7 to 2.6 seconds. The systems also return different artifacts. QMD returns matching
files and snippets for an agent to interpret, while Aizk returns one ranked prompt-ready evidence
string. Literal search and QMD therefore remain the better file-discovery tools, while Aizk is the
better current-state memory surface in this cell.

## Against published memory systems

| Capability | Zep and Graphiti | Mem0 | GraphRAG | Aizk |
|---|---|---|---|---|
| temporal facts | temporal graph | memory updates | no | valid and recorded ranges |
| consolidation | model-driven | add, update, delete | no | rules first, model on ambiguity |
| group speaker semantics | limited | user namespace | no | author snapshot and epistemic kind |
| authorization | application layer | application layer | no | forced PostgreSQL RLS |
| overlapping scopes | no | no | no | arbitrary nonempty scope sets |
| retrieval | graph and text | vector | community summaries | typed hybrid query and optional graph lanes |
| local operation | service oriented | optional | batch oriented | PostgreSQL and local model lanes |

This table compares mechanisms, not scores. The honest GroupMemBench adapter now exists, but the
full Aizk run has not been completed. External head-to-head claims wait for the same imported
histories, answer model, judge, and hardware budget across systems.

Recent evidence also argues against assuming that more graph machinery is better. The ACL 2026
study [Does Memory Need Graphs](https://aclanthology.org/2026.acl-long.1232/) finds that raw session
evidence plus independent summaries, facts, and keywords is a strong baseline. Similarity edges
can add noise, and graph summaries can improve retrieval metrics while reducing answer quality if
they crowd raw evidence out of the prompt. Aizk therefore keeps lane ablation and a flat baseline
on the roadmap.
