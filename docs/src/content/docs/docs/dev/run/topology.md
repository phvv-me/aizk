---
title: "Deployment topology"
description: "Every Compose service, what it does, and which profile starts it."
---

The whole deployment is one file, `src/deploy/docker-compose.yml`. This page walks it service by
service, so it helps to have read the [System map](/docs/dev/architecture/system-map/) first and
to know that aizk ships as three Python entrypoints on one image.

```d2
direction: right

clients: "MCP clients and browsers" { shape: cloud }

host: "one Docker host, Compose project aizk" {
  public: "profile public" {
    cloudflared
    caddy
    frontend: "frontend, SvelteKit"
    docs: "docs, static Astro"
    api
    logto
  }

  core: "default profile" {
    server
    worker
    setup
    db: "db, PostgreSQL 18 with VectorChord"
    objects: "objects, SeaweedFS"
    clamav
    docling
  }

  models: "default profile, on the GPUs" {
    emb: "vllm-emb"
    rerank: "vllm-rerank"
    llm: "vllm-llm"
    gliner
  }

  obs: "profile observability" {
    alloy
    loki
    grafana
  }
}

clients -> host.public.cloudflared
host.public.cloudflared -> host.public.caddy
host.public.cloudflared -> host.public.logto
host.public.caddy -> host.core.server: "MCP and OAuth"
host.public.caddy -> host.public.api: "upload PUTs"
host.public.caddy -> host.public.frontend: "app paths"
host.public.caddy -> host.public.docs: "everything else"
host.core.server -> host.core.db
host.core.worker -> host.core.db
host.core.server -> host.core.clamav
host.core.worker -> host.core.docling
host.core.worker -> host.core.objects
host.core.server -> host.models.gliner
host.core.server -> host.models.emb
host.core.server -> host.models.rerank
host.core.worker -> host.models.llm
host.obs.alloy -> host.obs.loki
host.obs.grafana -> host.obs.loki
```

## The default profile

These start on a plain `up -d` with no profile flag.

| Service | Image | What it does |
|---|---|---|
| `volume-init` | `aizk-runtime` | one-shot, prepares `/oauth` and `/backups` for UID 10001 |
| `db` | `tensorchord/vchord-suite:pg18-latest` plus digest | PostgreSQL 18, vectors, BM25, the queue |
| `objects` | `chrislusf/seaweedfs:4.29` plus digest | immutable artifact bytes over S3 |
| `clamav` | `clamav/clamav:1.5.3-debian13-slim` plus digest | scans bytes before they are stored |
| `docling` | `docling-serve-cpu:v1.26.0` plus digest | converts originals to Markdown and JSON |
| `vllm-emb` | `vllm/vllm-openai:v0.24.0` | the embedder |
| `vllm-rerank` | `vllm/vllm-openai:v0.24.0` | the cross-encoder rerank lane |
| `vllm-llm` | `vllm/vllm-openai:v0.24.0` | the extractor |
| `gliner` | built from `src/services/gliner/Dockerfile` | gate, mentions, optional extraction |
| `setup` | `aizk-runtime` | one-shot, `admin database setup` |
| `server` | `aizk-runtime` | the MCP server |
| `worker` | `aizk-runtime` | the queue drain, the scheduled passes, backups |

## The public profile

`--profile public` adds everything that faces the Internet.

`cloudflared` holds an outbound tunnel and publishes no port. `logto` is the identity provider on
its own hostname. `logto-setup` reconciles the committed authorization policy and `public-check`
refuses to finish when authentication is only half configured. `web-check` does the same for the
browser settings. `api` serves the browser JSON API, `frontend` is the SvelteKit server, and
`docs` serves the landing page and this documentation as one static Astro build. `caddy` is the
tunnel origin in front of all four.

`docs` is worth calling out. It is built from the `docs` target in `src/deploy/Dockerfile`, holds
no credential, and dials no other service, so it keeps answering while the engine is down.

## Caddy routing

`src/deploy/Caddyfile` listens on 8081 and matches in source order.

| Match | Goes to |
|---|---|
| `/mcp`, `/mcp/*`, the FastMCP OAuth routes, `/authorize`, `/token`, `/register`, `/auth/callback`, `/consent` | `server:8080` |
| `PUT /api/uploads/*` | `api:8010` |
| any other `/api` path | `404` |
| `/app/*`, `/auth/*`, `/events/*`, `/_app/*` | `frontend:3000` |
| everything else | `docs:8082` |

Order matters in exactly one place. `/auth/callback` belongs to the MCP server and is matched
first, so the later `/auth/*` rule only catches the browser sign-in flow. Because the fallback is
the static site, a new documentation page never needs a routing change.

:::caution[Keep the caddy `web` alias]
The `caddy` service declares a network alias of `web`, because the externally managed tunnel
ingress still targets `web:8081`. Rename the service without carrying that alias forward and the
tunnel breaks while every container still looks perfectly healthy.
:::

## The observability and integration profiles

`--profile observability` adds `observability-init`, `loki`, `alloy` and `grafana`, described on
[Observability](/docs/dev/run/observability/). Grafana is the only service in the whole file that
publishes a host port, and it is bound to `127.0.0.1`.

`--profile integration` starts one service, `artifact-integration`, built from the
`integration-test` Dockerfile target. It runs the real artifact suite against the real
PostgreSQL, SeaweedFS, ClamAV, Docling and GLiNER services rather than fakes, creates and drops
its own database, and exits. See [Testing](/docs/dev/contributing/testing/).

## Configuration and state

Every aizk service loads two environment files in order, `src/deploy/logto.conf` first and then
the project `.env`, so the committed nonsecret policy is the base and `.env` is the deployment
override. The Compose project name is pinned to `aizk` at the top of the file, because Compose
would otherwise derive it from the `deploy` directory and orphan the existing containers and
volumes onto a new project.

Eight named volumes hold the state, `db-data`, `object-data`, `clamav-data`, `backups`, `oauth`,
`loki-data`, `alloy-data` and `grafana-data`. Each one is also an environment variable, so a
production host points them at absolute directories on the right disk.
[PostgreSQL and storage](/docs/dev/run/postgres/) covers that layout and the ownership each one
needs.

## Startup order is enforced by healthchecks

The models are the interesting part. vLLM sizes each KV cache against the memory that is free
when it profiles, so three servers starting at once each read the whole card as free and their
sum overcommits it. The `depends_on` chain serializes them instead.

```text
  vllm-emb ──▶ vllm-rerank ──▶ vllm-llm ──▶ gliner
   healthy       healthy        healthy
```

Each one comes up against memory the previous lane already holds. Their healthchecks carry a 300
second `start_period` so a probe failing during weight loading is not counted against retries.

The application chain is separate. `volume-init` must exit cleanly and `db` must be healthy
before `setup` runs migrations, and `setup` must complete before `server`, `api` and `worker`
start. Under the public profile `cloudflared` must be healthy before `logto-setup`, because the
tunnel is what publishes Logto's canonical issuer, and `public-check` runs after that. `caddy`
waits for `server`, `api`, `frontend` and `docs` to all be healthy.

## The shared hardening anchor

Every service built from the aizk image inherits one YAML anchor, `x-aizk-runtime`.

```yaml
cap_drop: ["ALL"]
security_opt: ["no-new-privileges:true"]
read_only: true
tmpfs:
  - /tmp:rw,noexec,nosuid,size=1g
pids_limit: 512
```

`frontend`, `docs` and `caddy` repeat the same shape with smaller tmpfs and process caps. The one
exception is `volume-init`, which runs as root with exactly three capabilities, `CHOWN`,
`DAC_READ_SEARCH` and `FOWNER`, creates two mode `0700` directories for UID 10001, and exits
before anything long-lived starts.

Be honest about the rest. `db`, `objects`, `clamav`, the vLLM containers, `gliner`, `logto` and
`cloudflared` are upstream images running with default Docker capabilities. `docling` gets
`no-new-privileges` and a process cap but not the full anchor. The hardening covers the code we
wrote and not the whole host.

## Next

<div class="not-content">

- [First start](/docs/dev/run/first-start/) takes an empty host to a running deployment.
- [Hardware and cost](/docs/dev/run/hardware/) sizes the GPUs, RAM and disk this needs.
- [The security model](/docs/dev/run/security/) explains why the process split looks like this.
- [Observability](/docs/dev/run/observability/) covers the logging profile and the health command.

</div>
