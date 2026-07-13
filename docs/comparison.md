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
