---
title: "The eval CLI"
description: "Every evaluation command and the question it answers."
---

Every measurement on the pages around this one came out of one command tree. This page assumes
you have read [how we evaluate](/docs/dev/eval/approach/) and that you have a running deployment
with a corpus in it.

## How to run it

From the monorepo root, always through chefe.

```sh
chefe run aizk-eval <command> [--flags]
```

That task runs `python -m eval.launcher` inside `packages/aizk`, and the launcher is a five line
module that exists for exactly one reason. Three commands must never touch the live database, so
the launcher rewrites the environment before it execs `eval.cli`.

```text
  chefe run aizk-eval <command>
             │
             ▼
      eval/launcher.py
             │
   command in {extraction, groupmem, scale} ?
             │
      no ─────┴───── yes
      │              │
      │              AIZK_DB_NAME := AIZK_EVAL_DB_NAME (default aizk_eval)
      │              drop AIZK_DATABASE_URL and AIZK_ADMIN_DATABASE_URL
      ▼              ▼
   live corpus    isolated evaluation database, reset before the run
             │
             ▼
      python -m eval.cli   →  fire.Fire(EvaluationCLI)
```

The three isolated commands each call `EvaluationDatabase().reset()`, which refuses to run at all
unless the configured database name ends in `_eval`. That check is the last line of defense
between a benchmark and somebody's memory.

Every command returns a rendered text report on stdout and takes `--out <path>` to also write the
structured JSON. Every live command takes `--user <uuid5>` to select the corpus owner, defaulting
to `settings.system_user_id`, and `bench` raises rather than reporting zeros when that user can
see no evidence.

## The commands

| Command | Answers | Where it runs |
|---|---|---|
| `bench` | how good is retrieval on this deployment right now | live corpus |
| `freeze` | what is the fixed question set the regression gate uses | live corpus, writes files |
| `trace` | why did this one query return what it returned | live corpus |
| `management` | can every Area and Project find its own current brief | live corpus |
| `plans` | would any other retrieval plan or the retired router do better | live corpus |
| `gate` | what does the relevance gate save and what does it cost | live corpus |
| `extraction` | which model writes the better graph, and how fast | isolated |
| `groupmem` | how does aizk score on a published group memory benchmark | isolated |
| `scale` | at what corpus size does a component cross its latency budget | isolated |

## The live commands

**`bench`** scores the one plan production actually uses, `Plan.maximal()`, over freshly generated
stratified probes. Flags are `--k 8`, `--per-stratum 8` and `--strata local,global,multihop`.
[Retrieval results](/docs/dev/eval/retrieval/) explains the strata and the metrics.

**`freeze`** generates each stratum once and commits it, defaulting to
`tests/benchmark/data/retrieval_questions.jsonl` at `--per-stratum 100`. It writes a companion
`.sha256` file, and `load_frozen_corpus` refuses a corpus whose recomputed fingerprint does not
match, so the regression gate can never drift onto different questions without saying so.

**`trace`** takes a query as its one positional argument and shows statement rank, cross-encoder
merit and the packing cut for a single recall. It runs the real read path without updating access
history, so tracing a query never changes what the next recall would rank. Flags are `--k 8` and
`--budget`, which defaults to the production `context_token_budget` of 2048.

**`management`** discovers every visible Area and Project from declared source documents, then
runs twenty templated grounded questions for each one and checks where that subject's own current
brief landed. Flags are `--kinds area,project`, `--k 8` and `--budget`. Concurrency comes from
`AIZK_EVAL_CONCURRENCY`, which defaults to 4.

**`plans`** is the diagnostic study rather than a production measurement. It scores the maximal
plan beside `maximal_without_raptor`, `maximal_without_communities`, `maximal_without_profiles`
and `focused`, measures what the retired router would have chosen, and with `--seeding` also
sweeps the graph seeding arms. Add `--gate-limit N` to fold a gate replay into the same report.

**`gate`** replays the relevance gate over stored chunks and force-extracts everything it
rejected, so the report can show the extraction calls saved beside the facts that rejection cost.
It spends one bounded model call per rejected chunk, which is why `--limit` defaults to 50 and
why this never runs inside a build.

## The isolated commands

**`extraction`** is the only command whose first argument is a dataset. It takes a JSONL path of
human-verified cases and scores one explicit backend against them.

```sh
chefe run aizk-eval extraction /path/to/extraction-cases.jsonl \
  --backend llm --model gemma-4-31b --concurrency 1 --backlog 10704 \
  --out /tmp/extraction-llm.json
```

`--backend` is `llm` or `gliner` and `--model` is the model string actually sent to the configured
endpoint rather than a report label, so one URL and key can compare several hosted models.
`--concurrency` reproduces production fan-out and `--backlog` projects the completion ETA.
[Extraction and models](/docs/dev/eval/extraction/) has the metrics and the results.

**`groupmem`** runs the full external benchmark path against a released corpus directory.

```sh
chefe run aizk-eval groupmem /path/to/GroupMemBench --domain Finance --question-limit 2
```

Flags are `--domain Finance`, `--kinds` over the six question families, `--message-limit`,
`--question-limit`, `--k 10`, `--prepare` and `--keep`. Either limit marks the report diagnostic
rather than publishable, and the isolated scope is purged after the run unless you pass `--keep`.

**`scale`** grows a synthetic corpus through a list of sizes and reports where each component
crosses its budget. Flags are `--sizes 1000,10000`, `--k 8`, `--repeats 10` and
`--recall-p95-ms 200`. Each point records ingest throughput, recall p50, p95 and p99, per lane
latency, community detection time, table and index bytes, and peak host and GPU memory measured
through `mainboard`. The report ends with the flagged knees, which are the first size at which
each component went over budget.

## What a run costs

Three of these commands are cheap and the rest are not, which is worth knowing before you start
one on a laptop. `trace` is a single recall. `scale` and `management` spend database time rather
than model time, although `management` runs twenty recalls per subject and that multiplies fast on
a corpus with forty projects in it.

Everything else spends model calls. `bench`, `freeze` and `plans` generate one question per sample
with the LLM before they measure anything, and `plans` then replays every question through five
arms plus the seeding sweep plus a routing classification. `gate` spends one bounded extraction per
rejected chunk, and `extraction` and `groupmem` are model bound end to end. Reach for `--out` on
the expensive ones, because the JSON keeps the long-format per question rows and a paired analysis
afterward is free while a rerun is not.

## Settings that only the eval process reads

`EvaluationSettings` in `src/eval/config.py` uses the `AIZK_EVAL_` prefix and is separate from
application settings on purpose, so pointing a benchmark at a stronger judge never changes what
the deployment serves.

| Setting | Default | Effect |
|---|---|---|
| `AIZK_EVAL_DB_NAME` | `aizk_eval` | read by the launcher itself, the isolated database it reroutes to |
| `AIZK_EVAL_URL`, `AIZK_EVAL_API_KEY`, `AIZK_EVAL_MODEL` | inherit the app LLM | the answering model |
| `AIZK_EVAL_JUDGE_MODEL` | the answering model | the judging model |
| `AIZK_EVAL_JUDGE` | `false` | turn on answerability judging in the retrieval bench |
| `AIZK_EVAL_CONCURRENCY` | `4` | concurrent cases in the benchmarks that fan out |
| `AIZK_EVAL_MAX_TOKENS` | `512` | answer length cap for benchmark generation |

## Next

<div class="not-content">

- [Retrieval results](/docs/dev/eval/retrieval/) is what `bench`, `management` and `plans` found.
- [Extraction and models](/docs/dev/eval/extraction/) is what `extraction` found.
- [External benchmarks](/docs/dev/eval/external/) is what `groupmem` can and cannot claim.
- [Testing](/docs/dev/contributing/testing/) covers the ordinary suite and the coverage gate.

</div>
