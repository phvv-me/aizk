# CockroachDB development profile

This isolated profile runs AIZK with CockroachDB and OpenRouter. It does not join, stop, or
reuse any Crimson service, network, port, or volume. The existing PostgreSQL profile remains
unchanged.

Copy `.env.example` outside version control or pass the monorepo environment file explicitly.

```sh
docker compose \
  --env-file ../../.env \
  -f src/deploy/cockroachdb/docker-compose.yml \
  up --build
```

The MCP endpoint is available at `http://127.0.0.1:8088/mcp`. Cockroach SQL and its local
console bind only to loopback on ports `26258` and `8181`.

The optional `lambda` profile builds the same image used by AWS and runs its MCP and worker
entrypoints through the local Lambda runtime emulator. It keeps the normal server and worker
running and exposes the MCP, worker, and setup emulators only on loopback ports `9090`, `9091`,
and `9092`.

```sh
docker compose \
  --profile lambda \
  --env-file ../../.env \
  -f src/deploy/cockroachdb/docker-compose.yml \
  up -d --build lambda-mcp lambda-worker lambda-setup
```

Invoke the worker with an empty event.

```sh
curl -X POST \
  http://127.0.0.1:9091/2015-03-31/functions/function/invocations \
  -H 'content-type: application/json' \
  --data '{}'
```

The MCP emulator accepts API Gateway HTTP API version two events at the corresponding path on port
`9090`. The setup emulator accepts an empty event on port `9092` and safely reports an unchanged
migration head after the normal setup service has run. The Lambda image is about 902 MB locally.
ECR keeps only the two newest immutable images.

The profile sends embeddings to `qwen/qwen3-embedding-8b` and extraction to
`deepseek/deepseek-v4-flash`. Both requests require OpenRouter zero data retention and deny
data collection. Reranking stays disabled because no eligible zero data retention reranking
endpoint was available during the July 2026 validation.

## CockroachDB Cloud

The submission uses two CockroachDB tools. C-SPANN Distributed Vector Indexing powers every
embedded retrieval lane. The pinned `ccloud` image creates and inspects the managed memory
cluster. Building the image and checking its version are local and create no cloud resources.

```sh
chefe run ccloud-check
```

The following login is headless friendly. It persists the temporary CockroachDB Cloud login in
the isolated `aizk-cockroachdb-cloud` volume.

```sh
chefe run ccloud -- auth login --no-redirect
```

Creating the contest cluster is an explicit later deployment action. The command selects AWS
`us-east-1` beside the Lambda functions and fixes the CockroachDB Basic spend limit at zero.

```sh
chefe run ccloud -- cluster create basic aizk-memory us-east-1 --cloud AWS --spend-limit 0
chefe run ccloud -- cluster info aizk-memory
```

Create separate `aizk_admin` and `aizk_app` SQL users with `ccloud cluster user create`. In an
admin SQL shell, create database `aizk` and revoke the `admin` role from `aizk_app` before the
setup Lambda runs. Keep both generated passwords outside version control. Supply SQLAlchemy URLs
with `sslmode=verify-full`. AIZK translates the libpq TLS arguments for asyncpg and can accept an
optional PEM through `AIZK_DB_SSL_ROOT_CERTIFICATE` when the system trust store is insufficient.

Remove only this profile with the same file and project name.

```sh
docker compose -f src/deploy/cockroachdb/docker-compose.yml down
```

Include `--profile lambda` when the Lambda emulators are running.

Add `--volumes` only when the CockroachDB data in this isolated profile is intentionally being
discarded.
