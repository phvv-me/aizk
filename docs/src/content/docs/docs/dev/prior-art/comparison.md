---
title: "Choosing the right memory tool"
description: "When to use exact search, semantic file search, or aizk."
---

This page explains which problem aizk solves and when a simpler tool is a better choice. The
comparison is about behavior, not winners. No head-to-head benchmark supports the feature tables
below. [How we evaluate](/docs/dev/eval/approach/) explains which measurements can support a public
quality claim.

## Exact search, semantic file search, and aizk

A vault means a folder of Markdown files, notes, or project documents. Three tools cover different
ways of finding information in that folder.

- [`rg`](https://github.com/BurntSushi/ripgrep) searches exact text and file paths without an
  index.
- [`qmd`](https://github.com/tobi/qmd) indexes local documents for keyword and semantic search. It
  returns matching files or passages.
- aizk stores sources, claims, authorship, validity periods, and sharing boundaries. It returns a
  ranked set of evidence that an agent can use directly.

Use exact search whenever the wording or path is known. Use semantic file search when the wording
is uncertain but the matching document is the desired result. Use aizk when the answer also
depends on who said something, when it was true, who may read it, or how several sources fit
together.

```text
  a question
      │
      ▼
  exact wording or path known?
      │
      ├─ yes ─▶ rg      searches the files directly
      │
      └─ no  ─▶ is a matching file or passage enough?
                    │
                    ├─ yes ─▶ qmd    searches an indexed document collection
                    │
                    └─ no  ─▶ aizk   returns ranked, sourced evidence
```

| Need | Best starting point | Why |
|---|---|---|
| exact term or path | `rg` | it searches the files directly and needs no index |
| related passage with different wording | `qmd` | it combines keyword and semantic document search |
| current project decision | aizk | it can rank the maintained brief above related history |
| belief, preference, or observation | aizk | it keeps the statement attached to its speaker and kind |
| knowledge shared by two teams | aizk | one item can require membership in both organizations |
| earlier state of a fact | aizk | claims retain when they were valid and when they were recorded |
| sourced context for an agent | aizk | recall returns bounded evidence with source provenance |

### Reproducible comparison questions

Use synthetic fixtures when comparing retrieval systems. They make the inputs public and
reproducible without exposing a private corpus. Every example below is fictional and states the
retrieval behavior being tested.

| Question family | Example |
|---|---|
| exact lookup | find the release checklist by its title |
| current state | return the maintained Atlas migration brief rather than an older journal entry |
| similar titles | distinguish Atlas Migration from Atlas Migration Weekly Plan |
| shared decision | find the retry decision visible to the platform team |
| historical state | recover the policy that was valid before a rollback |
| multi-source evidence | gather the independent findings that support a cache change |

A fair comparison must score the result each tool promises. File search succeeds when it returns
the right file or passage. aizk succeeds when the authorized evidence comes from the right sources
and reflects the requested history. Mixing those contracts into one score would hide what each
system actually did.

## Published memory systems

The systems below overlap with aizk, but none has the same boundary.

- [Zep and Graphiti](https://arxiv.org/abs/2501.13956) organize changing facts in a temporal
  knowledge graph.
- [Mem0](https://arxiv.org/abs/2504.19413) maintains compact user memories through add, update, and
  delete decisions.
- [GraphRAG](https://arxiv.org/abs/2404.16130) builds graph communities and summaries to answer
  questions over document collections.
- aizk keeps original sources beside attributed and time-bounded claims, then applies database row
  security before retrieval.

| Capability | Zep and Graphiti | Mem0 | GraphRAG | aizk |
|---|---|---|---|---|
| changing facts | temporal graph | memory replacement | not its focus | separate validity and recording ranges |
| conflicting speakers | limited representation | separate user memories | not its focus | attributed claims with statement kinds |
| access control | handled by the application | handled by the application | handled by the application | forced row security in the database |
| overlapping organizations | handled by the application | handled by the application | handled by the application | one item may require several memberships |
| primary retrieval unit | graph facts and text | compact memories | community summaries | original passages, claims, and optional graph projections |
| deployment shape | memory service | hosted or self-hosted service | offline indexing pipeline | SQL database with replaceable model services |

:::caution
The table describes architectural responsibilities. It makes no answer-quality claim.
:::

The repository includes a GroupMemBench adapter at `src/eval/groupmem.py`. A publishable comparison
would still need every system to receive the same histories and questions under the same answer
model, judge, and resource budget. No such result is published. [External
benchmarks](/docs/dev/eval/external/) defines that work.

## Why graph retrieval stays optional

Building a graph does not guarantee a better answer. The study
[Does Memory Need Graphs](https://aclanthology.org/2026.acl-long.1232/) finds that raw session
evidence plus independent summaries, facts, and keywords is already a strong baseline. Similarity
edges can add noise, and graph summaries can improve retrieval measures while making the final
answer worse because the summary displaces original evidence from the prompt.

For that reason, aizk keeps passages from the original source as primary evidence. Graph facts,
profiles, and summaries are replaceable aids. Each one must improve answer quality in an ablation,
which means comparing the same retrieval process with that aid enabled and disabled. [Retrieval
results](/docs/dev/eval/retrieval/) records those comparisons.

## Next

<div class="not-content">

- [References and lineage](/docs/dev/prior-art/references/) maps each mechanism to its source and its code.
- [Rejected and deferred](/docs/dev/prior-art/rejected/) records which ideas were not adopted and why.
- [How we evaluate](/docs/dev/eval/approach/) explains what counts as a measurement here.

</div>
