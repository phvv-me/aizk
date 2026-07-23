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

The submission uses three CockroachDB tools. C-SPANN Distributed Vector Indexing powers every
embedded retrieval lane. The pinned `ccloud` image inspects and manages the cluster. The managed
CockroachDB Cloud MCP server gives the operator a guarded schema and query surface. Building the
CLI image and checking its version are local and create no cloud resources.

```sh
chefe run ccloud-check
```

The following login is headless friendly. It persists the CockroachDB Cloud login in the isolated
`aizk-cockroachdb-cloud` volume.

```sh
chefe run ccloud -- auth login --no-redirect
```

The contest cluster is a CockroachDB Cloud cluster in AWS Singapore. Keep Lambda in
`ap-southeast-1` beside it. Inspect the existing cluster after authenticating rather than creating
a second one.

```sh
chefe run ccloud -- cluster list
chefe run ccloud -- cluster info aizk-cockroachdb
```

Keep the console-created `aizk` SQL user as the migration owner. Create `aizk_app` separately, then
revoke its default `admin` membership before the setup Lambda runs. The Cloud UI and `ccloud`
create SQL users as administrators by default, so this revocation is required for row security to
have a meaningful application boundary.

```sh
chefe run ccloud -- cluster user create aizk-cockroachdb aizk_app
```

Run the following statement as `aizk`, followed by `CREATE DATABASE aizk` if the dedicated database
does not exist yet.

```sql
REVOKE admin FROM aizk_app;
GRANT CONNECT ON DATABASE aizk TO aizk_app;
```

Keep both passwords outside version control. Set complete `AIZK_ADMIN_DATABASE_URL` and
`AIZK_DATABASE_URL` values as shown in `.env.example`. Both URLs use `sslmode=verify-full`. Local
tools can retain the downloaded `sslrootcert` path. The Lambda image already trusts the downloaded
ISRG Root X1 through its system CA bundle, so its SSM URLs omit the machine-specific path.

The CockroachDB migration history is intentionally one fresh `0001_cockroachdb` baseline. It
creates the current tables, portable queue, row security, full-text index, C-SPANN indexes, views,
and monthly quota counters. PostgreSQL keeps its independent migration history.

The managed MCP connection uses `https://cockroachlabs.cloud/mcp` with the cluster ID header. Start
with read access during OAuth authorization. It is an operator and contest surface, not part of
the user request path and not a replacement for the application SQL roles.

The Cloud Console Jobs page shows CockroachDB internal work such as schema changes, index builds,
statistics, backups, and imports. It does not execute AIZK background tasks. Those remain durable
rows in `queue_task` and `queue_event`, drained by the Lambda worker and recovered every 15 minutes.

Remove only this profile with the same file and project name.

```sh
docker compose -f src/deploy/cockroachdb/docker-compose.yml down
```

Include `--profile lambda` when the Lambda emulators are running.

Add `--volumes` only when the CockroachDB data in this isolated profile is intentionally being
discarded.
