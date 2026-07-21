---
title: "Development setup"
description: "Getting a working checkout, a database, and the model lanes."
---

This page gets you from a fresh clone to a green test run. It assumes Docker and git, and nothing
about aizk itself. [Repository tour](/docs/dev/architecture/repository/) explains what you will
find inside `src/`, and [First start](/docs/dev/run/first-start/) covers bringing up a real
deployment rather than a development one.

## chefe owns everything

There is one rule and it saves a lot of confusion. Every command runs through `chefe run`. Never
call `uv run`, `pip`, `pytest`, `python`, or `pixi` directly, because the environment those would
use is not the environment the gate uses, and the divergence is not theoretical. CI once ran a
separate `uv sync`, and a stale lock plus three missing type stubs hid 187 type errors from the
local gate for weeks.

```text
  pyproject.toml
    [project.dependencies]     runtime deps
    [dependency-groups].dev    checkers, pytest, hypothesis, eval stack
    [tool.chefe]               interpreter, git sources, task list
            │
            │  chefe install
            ▼
  .chefe/pixi.toml  ──▶  .chefe/.pixi/envs/default   one solved env
            │
            │  chefe run <task>
            ▼
     lint · typecheck · lint-imports · test · migrate
```

Bootstrap is two commands.

```sh
uv tool install "chefe>=0.0.25"
chefe install
```

`chefe install` compiles the `[tool.chefe]` table, `[project.dependencies]`, and
`[dependency-groups].dev` into `.chefe/pixi.toml` and solves one environment from them. CI runs the
exact same two commands, so a green local gate means a green CI gate.

## The sibling house packages

Three house packages sit under aizk and matter when you change them.

`patos` supplies the typed base models and the `patos.sql` column primitives that every store model
is built from. `rlsalchemy` is the row level security engine, and note that the distribution is
named `rlsalchemy` while the import is `rls`, which trips people up once each. `mainboard` supplies
the hardware probe and the profiler.

In a standalone aizk checkout, `[tool.chefe.sources]` routes all three to the `main` branch of their
git repositories, because PyPI lags their source at the same version number. The practical
consequence is ordering. If a change needs a new `rls` behavior, push `rls` first, then push aizk,
or CI resolves the old HEAD and fails on something that works locally. Inside the monorepo the same
three are editable path dependencies, so an edit is live immediately with no push at all.

## A database

The suite and the application both want a real PostgreSQL with the VectorChord extensions, so the
Compose `db` service is the shortest path. Copy the environment template first.

```sh
cp src/deploy/.env.example .env
```

Fill in `AIZK_ADMIN_PASSWORD`, `AIZK_APP_PASSWORD`, `AIZK_LOGTO_DB_PASSWORD`, the two
`AIZK_OBJECT_STORE_*` keys, and `AIZK_DOCLING_API_KEY`. Every one of those is required and Compose
refuses to start without them. Then bring up the database alone.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml up -d db
```

That runs `tensorchord/vchord-suite:pg18-latest` on port 5433. On the very first start,
`src/deploy/initdb/roles.sh` creates the restricted `aizk_app` login role, and this matters more
than it looks. `aizk_app` is `NOBYPASSRLS`, so development exercises the same forced row level
security that production does, rather than quietly running as an owner who can see everything.
[PostgreSQL and storage](/docs/dev/run/postgres/) has the rest of the configuration.

Apply the schema with `chefe run migrate-aizk`. The test suite does not need this step, because it
creates and migrates its own database per process, which
[Testing](/docs/dev/contributing/testing/) explains.

## The model lanes

You can be productive with only PostgreSQL. The suite is hermetic above the database seam, so
`tests/conftest.py` points the embedder, reranker, gate, and extraction model at in-process doubles
for every test. Nothing reaches a live service and no GPU is required to run the gate.

Real ingestion and real recall need the sidecars, and they are ordinary Compose services you can
start selectively.

| Service | Lane | Setting |
|---|---|---|
| `vllm-emb` | embedding | `AIZK_EMBED_URL`, default `http://localhost:8000/v1` |
| `vllm-rerank` | cross-encoder rerank | `AIZK_RERANK_URL`, default `http://localhost:8004` |
| `vllm-llm` | graph extraction | `AIZK_EXTRACT_BACKEND=llm` |
| `gliner` | the cheap entity gate | `AIZK_GLINER_URL`, default `http://localhost:8006` |
| `docling` | file conversion | `AIZK_DOCLING_API_KEY` |
| `clamav` | fail-closed malware scan | `AIZK_CLAMAV_*` |
| `objects` | SeaweedFS artifact bytes | `AIZK_OBJECT_STORE_*` |

Rerank is part of every recall now rather than an optional pass, so a working rerank endpoint is
needed for anything past the test doubles. `AIZK_EXTRACT_BACKEND` switches between the production
LLM extractor and the experimental GLiNER graph route without any code change, which is the knob to
reach for when comparing them.

## The tasks you actually need

From the monorepo root the aizk tasks carry an `-aizk` suffix, since the root manifest holds every
package.

| Task | What it does |
|---|---|
| `chefe run test-aizk` | the fast suite, four workers, no coverage |
| `chefe run test-aizk-cov` | the same suite plus the 100 percent coverage gate |
| `chefe run typecheck-aizk` | pyrefly and ty over `src` |
| `chefe run lint-imports-aizk` | the layered import contracts |
| `chefe run lint` | ruff, formatting, spelling, and the rest of pre-commit |
| `chefe run migrate-aizk` | apply pending migrations |
| `chefe run makemigrations-aizk -- "add foo column"` | autogenerate a migration from the model diff |
| `chefe run test-aizk-artifact-stack` | the real-services integration run |
| `chefe run docs-aizk` | build this documentation site and run its page gate |
| `chefe run aizk-web-check` | svelte-check, prettier, and the web smoke tests |
| `chefe run aizk-eval` | the evaluation and diagnostics CLI |
| `chefe run rls-report` | a posture snapshot of the live policies |

A standalone aizk checkout uses the shorter names from the package's own `[tool.chefe.tasks]`,
which are `chefe run lint`, `chefe run lint-imports`, `chefe run typecheck`, and `chefe run test`.
Those four are what CI executes.

While editing, the fastest loop is a focused run without coverage.

```sh
chefe run -- pytest tests/store/test_rls.py --no-cov
chefe run -- ruff check src/aizk/store
```

## Next

<div class="not-content">

- [Testing](/docs/dev/contributing/testing/) explains the fixtures, the fakes, and the coverage gate.
- [Style and typing](/docs/dev/contributing/style/) covers what the linters and checkers enforce.
- [Repository tour](/docs/dev/architecture/repository/) maps the packages you will be editing.

</div>
