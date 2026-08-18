---
title: "Development setup"
description: "Getting a working checkout, a database, and the model lanes."
---

This page gets you from a fresh clone to a green test run. It assumes Docker and git, and nothing
about aizk itself. [Repository tour](/docs/dev/architecture/repository/) explains what you will
find inside `src/`, and [First start](/docs/dev/run/first-start/) covers bringing up a real
deployment rather than a development one.

## One locked Python environment

`pyproject.toml` declares every Python dependency and `uv.lock` records the exact working solve.
CI installs that lock directly.

:::danger[Keep the environment frozen]
Run `uv sync --frozen --all-groups` once, install the reviewed SQLAlchemy revision, then use
`uv run --no-sync` for every check. A later implicit sync would replace the reviewed fork with the
registry wheel and remove the row security API AIZK is exercising.
:::

```text
  pyproject.toml
    [project.dependencies]     runtime deps
    [dependency-groups].dev    checkers, pytest, hypothesis, eval stack
            │
            │  uv sync --frozen --all-groups
            ▼
       uv.lock  ──▶  .venv   one solved env
            │
            │  scripts/install-sqlalchemy.sh
            ▼
   reviewed SQLAlchemy fork, then lint · typecheck · test
```

Bootstrap is two commands.

```sh
uv sync --frozen --all-groups
sh scripts/install-sqlalchemy.sh
```

The bootstrap script builds one immutable public SQLAlchemy revision and installs its wheel into
`.venv`. It fails if a supplied local checkout is on any other revision.

## The house packages

Two independently published project libraries sit below AIZK in the dependency graph.

`patos` supplies the typed base models and the `patos.sql` column primitives that every store model
is built from. `rlsalchemy` is the row level security engine, and note that the distribution is
named `rlsalchemy` while the import is `rls`.

`patos` and `rlsalchemy` are exact pins in `pyproject.toml`. The SQLAlchemy row security fork is an
exact commit pin in `scripts/install-sqlalchemy.sh` and the production image builds the same source.

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

Apply the schema with `uv run --no-sync aizk admin database migrate`. The test suite does not need this step, because it
creates and migrates its own database per process, which
[Testing](/docs/dev/contributing/testing/) explains.

## The model lanes

You can be productive with only PostgreSQL. The suite is hermetic above the database seam, so
`tests/conftest.py` points the embedder, reranker, gate, and extraction model at in-process doubles
for every test. Nothing reaches a live service and no GPU is required to run the gate.

Real ingestion and real find need the sidecars, and they are ordinary Compose services you can
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

Rerank is part of every find now rather than an optional pass, so a working rerank endpoint is
needed for anything past the test doubles. `AIZK_EXTRACT_BACKEND` switches between the production
LLM extractor and the experimental GLiNER graph route without any code change, which is the knob to
reach for when comparing them.

## The commands you actually need

| Command | What it does |
|---|---|
| `uv run --no-sync python -m pytest -n 4 --dist loadscope --benchmark-disable` | the fast parallel suite |
| `uv run --no-sync pyrefly check` | source and test typing |
| `uv run --no-sync ty check --python .venv --exit-zero-on-warning` | independent source typing |
| `uv run --no-sync mypy src/aizk src/eval` | strict source typing |
| `uv run --no-sync lint-imports` | the layered import contracts |
| `uv run --no-sync ruff check .` | Python linting |
| `uv run --no-sync aizk admin database migrate` | apply pending migrations |
| `pnpm --dir docs check && pnpm --dir docs build` | check and build the documentation |
| `pnpm --dir src/web check && pnpm --dir src/web test` | check and test the web application |
| `uv run --no-sync aizk-eval` | the evaluation and diagnostics CLI |

While editing, the fastest loop is a focused run without coverage.

```sh
uv run --no-sync python -m pytest tests/store/test_rls.py --no-cov
uv run --no-sync ruff check src/aizk/store
```

## Next

<div class="not-content">

- [Testing](/docs/dev/contributing/testing/) explains the fixtures, the fakes, and the coverage gate.
- [Style and typing](/docs/dev/contributing/style/) covers what the linters and checkers enforce.
- [Repository tour](/docs/dev/architecture/repository/) maps the packages you will be editing.

</div>
