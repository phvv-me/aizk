---
title: "Repository tour"
description: "What lives where, and which directory to open for a given change."
---

This page is the directory map, the one to keep open while you find your way around. It assumes
you have read the [System map](/docs/dev/architecture/system-map/), so it tells you where code
lives rather than what it does. [Layers and import contracts](/docs/dev/architecture/layers/) says
which of these directories may import which.

```text
  packages/aizk/
  ├── src/aizk/        the engine, one installable Python package
  ├── src/eval/        the benchmark and diagnostic harness, a second package
  ├── src/services/    the GLiNER sidecar, its own container
  ├── src/web/         the SvelteKit app
  ├── src/deploy/      compose file, Dockerfiles, Caddy, observability
  ├── docs/            this site
  └── tests/           one directory per engine package
```

## `src/aizk/`, the engine

Twenty-nine top-level modules, grouped here by what they are for rather than by layer.

| Group | Modules | What lives there |
|---|---|---|
| transports | `mcp/`, `api/`, `cli.py`, `commands/`, `client/` | the four ways in |
| the shared service | `memory.py` | `recall`, `remember` and `share` for one caller |
| the store | `store/` | models, mixins, DDL, migrations, engine, identity |
| the write path | `extract/`, `artifacts/`, `serving/` | ingest, uploads, model clients |
| derived knowledge | `graph/`, `ontology/` | extraction, grounding, communities, vocabulary |
| the read path | `retrieval/` | lanes, fusion, reranking, packing, templates |
| autonomy | `background/` | the PgQueuer wrapper, jobs, the scheduler |
| operations | `ops/`, `admin.py`, `backup.py`, `export.py`, `status.py` | doctor, probes, dumps, usage reports |
| foundations | `config/`, `types.py`, `exceptions.py`, `provenance.py`, `common/` | settings and leaf vocabulary |
| glue | `runtime.py`, `auth.py`, `storage.py`, `integrations/`, `usage.py` | composition, identity, bytes, sidecar clients |

A few of those deserve a sentence.

`store/` is the largest and the most structured. `models/tables/` has one file per table,
`models/views/` has the security-invoker views such as `live_fact.py`, `mixins/` assembles every
table from reusable pieces, `ddl/` holds the custom SQLAlchemy DDL constructs for extensions,
grants and views, `identity/` holds `User` and `Organization`, and `migrations/versions/` holds
exactly two revisions, `0001_init` and `0002_durable_usage`.

`serving/` is where the model clients live, one subpackage per lane, so `embed/`, `rerank/`,
`gate/`, `extract/` and `chunk/`. Every one of them talks to a container over HTTP and none of
them loads a model in-process.

`integrations/` is the same idea for non-model services, with `clamav/`, `docling/` and `logto/`.
Each has a `client.py` and typed `models.py`, so a sidecar's wire format never leaks into the
engine.

`retrieval/` splits into `lanes/`, `recall/` for the orchestrator, `rerank/`, `packing/` for the
budget walk, `models/` for the candidate and result types, and `templates/` for the single Jinja
template that renders the answer.

`graph/` is flat and each file is one step or one pass, which makes it the easiest package to read
end to end. `build.py` runs the per-chunk projection, `grounding.py` accepts only the proposed facts it can
tie back to the source text, `dedupe.py` and `consolidation.py` fold what survives into what is
already known, and `communities.py`, `raptor.py`, `profiles.py`, `insight.py`, `decay.py`, `promote.py` and
`reembed.py` are the scheduled passes.

`background/` is small on purpose. `queue.py` wraps PgQueuer with the typed `QueueJob` and
`QueuePayload` bases, `jobs/` holds the three job families for conversion, projection and
maintenance, and `schedule.py` binds them all onto one worker and fans the scoped passes out over
every distinct scope set that has stored memory.

## The other three source trees

`src/eval/` is a separate installable package with its own entrypoint, `aizk-eval`. It holds the
corpus builders, the retrieval and extraction runners, the metrics and statistics code, and a
small FastAPI service for running plans. It imports the engine and the engine never imports it.
[How we evaluate](/docs/dev/eval/approach/) covers it properly.

`src/services/gliner/` is two files, `app.py` and a `Dockerfile`. It is the only model server we
write ourselves, because GLiNER has no vLLM-compatible serving image. Everything else rides on
vLLM.

`src/web/` is the SvelteKit app. `src/lib/api/` is generated from `openapi.json`, which itself
comes from the FastAPI app, so a change to a browser API response type is regenerated rather than
hand-edited. `src/routes/app/` has one directory per screen, and those directories are where the
user-facing renaming shows up, since findings are facts, subjects are entities and themes are
communities. [The web app](/docs/dev/interfaces/web/) has the detail.

`src/deploy/` is the deployment. `docker-compose.yml` defines every container, `Dockerfile` builds
the runtime image, `Caddyfile` and `Caddyfile.docs` front the site, `initdb/roles.sh` creates the
database roles, and `observability/` holds the Alloy, Loki and Grafana configuration.

## `docs/` and `tests/`

`docs/` is this Astro and Starlight site. Pages live under `src/content/docs/docs/`, the sidebar
and integrations are in `astro.config.mjs`, interactive diagrams are Svelte components in
`src/components/`, the marketing landing page is `src/pages/index.astro` with its parts in
`src/components/marketing/`, and `scripts/check-pages.mjs` is the gate that fails the build on a
long page, a page with no diagram, or a broken link.
[Writing these docs](/docs/dev/contributing/docs-style/) is the contract.

`tests/` mirrors the engine, so `tests/store/`, `tests/graph/`, `tests/retrieval/` and so on, with
shared fixtures in `conftest.py` and factories in `factories.py` and `strategies.py`. Tests marked
`integration` and `benchmark` are excluded by default. Coverage is gated at 100 percent.
[Testing](/docs/dev/contributing/testing/) explains the layout and the markers.

## If you want to change X, open Y

:::tip[The fastest way in]
When you know the change but not the file, skim this table first. It is the shortcut past the
whole tree.
:::

| You want to change | Open |
|---|---|
| a table, a column, a policy | `src/aizk/store/models/tables/` then add a migration |
| what an MCP tool accepts or returns | `src/aizk/mcp/server.py` |
| what the web app can ask for | `src/aizk/api/app.py`, then regenerate the TS client |
| how a file becomes text | `src/aizk/artifacts/` and `src/aizk/integrations/docling/` |
| how text becomes chunks | `src/aizk/serving/chunk/` |
| which chunks get extracted | `src/aizk/serving/gate/` and `src/aizk/extract/` |
| how facts are grounded or merged | `src/aizk/graph/grounding.py`, `consolidation.py` |
| a retrieval lane | `src/aizk/retrieval/lanes/` |
| how results are ranked or packed | `src/aizk/retrieval/rerank/`, `packing/` |
| the wording of a recall response | `src/aizk/retrieval/templates/recall.md.j2` |
| a scheduled pass or its priority | `src/aizk/background/jobs/`, `schedule.py` |
| a setting or its default | `src/aizk/config/settings.py` |
| which services run | `src/deploy/docker-compose.yml` |
| a CLI command | `src/aizk/commands/` |
| a dependency or a task | `chefe.toml` at the monorepo root |

## What it is built on

aizk is one package in a monorepo and it leans on three sibling house packages instead of
reinventing them. `patos` supplies the typed base models, so `Model`, `FrozenModel` and the SQL
field helpers. `rls`, distributed as `rlsalchemy`, owns all the generic row level security
machinery, and aizk registers its tables with it and keeps only the scope lattice locally.
`mainboard` supplies the profiling spans you will see as `from mainboard.profiling import span` in
the graph and recall hot paths.

`chefe` owns dependencies and tasks. Every command in these docs is `chefe run something` from the
monorepo root, never a bare `python`, `pip`, `pytest` or `pixi`. Run `chefe tree` to see what is
available.

## Next

<div class="not-content">

- [Design principles](/docs/dev/architecture/principles/) explains why the tree is shaped this way.
- [Development setup](/docs/dev/contributing/setup/) gets a working environment.
- [The data model](/docs/dev/store/data-model/) is the right first stop inside `store/`.

</div>
