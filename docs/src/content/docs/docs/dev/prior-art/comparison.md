---
title: "Comparison"
description: "How aizk differs from vault search and from published memory systems."
---

This page compares mechanisms. It is not a scoreboard. Where a number appears it carries the date
and the conditions it was taken under, and where no number exists the page says so plainly. For
what has actually been measured and how, [How we evaluate](/docs/dev/eval/approach/) owns that
subject.

## Against vault search

Literal search stays the right tool for an exact string or a file path. `rg` finds it in
milliseconds and nothing has to be indexed first. aizk earns its keep on the other kind of
question, the one that needs meaning, source time, a speaker's perspective, a shared scope, or
evidence gathered across several documents. They are complements and a working setup runs both.

```text
  a question
      │
      ▼
  exact string or file path?
      │
      ├─ yes ─▶ rg      fastest, no index to keep fresh
      │
      └─ no  ─▶ needs a scope, a speaker, or a point in time?
                    │
                    ├─ no  ─▶ qmd    returns matching files and snippets
                    │
                    └─ yes ─▶ aizk   returns one ranked evidence pack
```

| Need | Vault tools | aizk |
|---|---|---|
| exact term or path | fastest and simplest | unnecessary overhead |
| paraphrased intent | embedding search where available | dense and lexical fusion in one statement |
| shared project memory | no authorization model | Logto-derived scope lattice |
| overlap of two organizations | manual duplication | native scope intersection |
| speaker belief or preference | prose to interpret | captured and attributed perspective |
| point-in-time replay | Git archaeology | bi-temporal range query |
| sourced agent context | manual note assembly | budgeted MCP context pack |

### One dated cell, 2026-07-15

The conditions come first, because they are what make the cell worth keeping. The full `qmd` index
was refreshed after the management notes had been normalized, then both systems were asked the
same eight representative questions. This was a manual check of which document came back first,
not an answer-generation benchmark, and it ran on one machine on one day.

aizk ranked the intended current brief first on eight of the eight questions. `qmd` without
reranking did so on five.

| Question family | aizk first | qmd first |
|---|---:|---:|
| current open Projects | yes | yes |
| current aizk state | yes | yes |
| current Japanese Area state | yes | no |
| JLPT N2 Window Weekly Plan | yes | yes |
| Personal Brand and Career Website | yes | yes |
| whether graph memory improves answers | yes | no |
| evaluating obsolete memory | yes | yes |
| next action for My Personal Computer | yes | no |

`qmd` stayed excellent whenever the question named one exact Project or one durable note. Its three
misses were broad current-state questions where a related journal or Area note outranked the
authority, plus one conceptual question where a note about CAGRA graph indexes outranked the
memory-research note. What made those cases reliable in aizk was managed-document identity,
status-aware catalogs, civil source dates, and maximal-title authority, so no query router was
needed.

Latency is the honest weak spot of the cell. The only stable `qmd` path was `--no-gpu --no-rerank`,
and it ranged from about 1.2 to 29.6 seconds with heavy variance on cold requests. CUDA
initialization kept failing because CMake could not resolve `CUDA::cublas`, and reranking sometimes
announced its stage and then returned nothing. Sequential aizk recalls over the same questions took
about 0.7 to 2.6 seconds. The two systems also hand back different things. `qmd` returns files and
snippets for an agent to interpret, while aizk returns one ranked prompt-ready evidence string. So
literal search and `qmd` remain the better file-discovery tools, and aizk was the better
current-state memory surface in this cell.

## Against published memory systems

| Capability | Zep and Graphiti | Mem0 | GraphRAG | aizk |
|---|---|---|---|---|
| temporal facts | temporal graph | memory updates | no | valid and recorded ranges |
| consolidation | model-driven | add, update, delete | no | rules first, model on ambiguity |
| speaker semantics in a group | limited | user namespace | no | author snapshot and epistemic kind |
| authorization | application layer | application layer | no | forced PostgreSQL RLS |
| overlapping scopes | no | no | no | arbitrary nonempty scope sets |
| retrieval | graph and text | vector | community summaries | typed hybrid plan and graph lanes |
| local operation | service oriented | optional | batch oriented | PostgreSQL plus local model lanes |

:::caution
Read that table as a description of mechanisms and nothing more. No head-to-head benchmark exists
behind it.
:::

An honest GroupMemBench adapter lives in the repository at `src/eval/groupmem.py`, but the full aizk
run has not been completed, and an external claim would need the same imported histories, the same
answer model, the same judge, and the same hardware budget on every system before it meant anything.
[External benchmarks](/docs/dev/eval/external/) tracks that work.

## More graph is not automatically better

It would be easy to read the table above as an argument that aizk wins by carrying more graph
machinery. The evidence says otherwise. The ACL 2026 study
[Does Memory Need Graphs](https://aclanthology.org/2026.acl-long.1232/) finds that raw session
evidence plus independent summaries, facts, and keywords is already a strong baseline. Similarity
edges can add noise, and graph summaries can lift retrieval metrics while lowering answer quality,
because the summary crowds the raw evidence out of the prompt.

That is why source chunks stay the primary evidence in every recall, why each graph lane has to
earn its cost in ablation instead of being assumed useful, and why a flat baseline over raw
messages, summaries, facts, and keywords sits on the roadmap.
[Retrieval results](/docs/dev/eval/retrieval/) is where those ablations land.

## Next

<div class="not-content">

- [References and lineage](/docs/dev/prior-art/references/) maps each mechanism to its source and its code.
- [Rejected and deferred](/docs/dev/prior-art/rejected/) records what did not survive contact with evidence.
- [How we evaluate](/docs/dev/eval/approach/) explains what counts as a measurement here.

</div>
