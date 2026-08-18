# CockroachDB development profile

This isolated profile runs AIZK with CockroachDB and OpenRouter. It does not join, stop, or
reuse any existing service, network, port, or volume. The PostgreSQL profile remains
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

Invoke the worker with an explicit event kind.

```sh
curl -X POST \
  http://127.0.0.1:9091/2015-03-31/functions/function/invocations \
  -H 'content-type: application/json' \
  --data '{"kind":"worker"}'
```

The local Lambda runtime emulator accepts one active invocation per container. Invoke each
emulator serially. Testing AWS horizontal concurrency requires one emulator container per
concurrent invocation. The profile disables async connection pooling across Lambda event loops,
and the worker uses the same eight-task batch size as the AWS stack.

The MCP emulator accepts Lambda Function URL version two events at the corresponding path on port
`9090`. The setup emulator uses the same worker handler on port `9092` and accepts
`{"kind":"setup"}`. It safely reports an unchanged migration head after the normal setup service
has run. The Lambda image is about 902 MB locally.
ECR keeps only the two newest immutable images.

The profile sends embeddings to `qwen/qwen3-embedding-8b` and asks OpenRouter to route each
embedding request to the provider with the best recent latency. Extraction uses the dated
`deepseek/deepseek-v4-flash-0731` slug. Embedding, extraction, and scientific figure descriptions
all use `AIZK_DEMO_OPENROUTER_API_KEY` without provider retention restrictions. OpenRouter had no
embedding endpoint compatible with the earlier zero data retention filter, which made every text
write fail before reaching CockroachDB. Only public demonstration documents belong in this
profile. Reranking stays disabled because this deployment does not need another paid lane.

## CockroachDB Cloud tooling

The cloud profile uses three CockroachDB tools. C-SPANN Distributed Vector Indexing powers every
embedded retrieval lane. The pinned `ccloud` image inspects and manages the cluster. The managed
CockroachDB Cloud MCP server gives the operator a guarded schema and query surface. Building the
CLI image and checking its version are local and create no cloud resources.

```sh
docker compose -f src/deploy/cockroachdb/docker-compose.ccloud.yml build ccloud
docker compose -f src/deploy/cockroachdb/docker-compose.ccloud.yml run --rm ccloud version
```

The following login is headless friendly. It persists the CockroachDB Cloud login in the isolated
`aizk-cockroachdb-cloud` volume.

```sh
docker compose -f src/deploy/cockroachdb/docker-compose.ccloud.yml \
  run --rm ccloud auth login --no-redirect
```

The target cluster is a CockroachDB Cloud cluster in AWS Singapore. Keep Lambda in
`ap-southeast-1` beside it. Inspect the existing cluster after authenticating rather than creating
a second one.

```sh
docker compose -f src/deploy/cockroachdb/docker-compose.ccloud.yml run --rm ccloud cluster list
docker compose -f src/deploy/cockroachdb/docker-compose.ccloud.yml \
  run --rm ccloud cluster info aizk-cockroachdb
```

Keep the console-created `aizk` SQL user as the migration owner. Create `aizk_app` separately, then
revoke its default `admin` membership before the worker receives its explicit setup event. The
Cloud UI and `ccloud` create SQL users as administrators by default, so this revocation is required
for row security to have a meaningful application boundary.

```sh
docker compose -f src/deploy/cockroachdb/docker-compose.ccloud.yml \
  run --rm ccloud cluster user create aizk-cockroachdb aizk_app
```

Run the following statement as `aizk`, followed by `CREATE DATABASE craizk_staging` if the
dedicated database does not exist yet.

```sql
REVOKE admin FROM aizk_app;
GRANT CONNECT ON DATABASE craizk_staging TO aizk_app;
```

Keep both passwords outside version control. Set complete `AIZK_ADMIN_DATABASE_URL` and
`AIZK_DATABASE_URL` values as shown in `.env.example`. Both URLs use `sslmode=verify-full`. Local
tools can retain the downloaded `sslrootcert` path. The Lambda image already trusts the downloaded
ISRG Root X1 through its system CA bundle, so its SSM URLs omit the machine-specific path.

The CockroachDB migration history is intentionally one fresh `0001_cockroachdb` baseline. It
creates the current tables, portable queue, row security, full-text index, C-SPANN indexes, views,
monthly quota counters, and application role grants. PostgreSQL keeps its independent migration
history. The local profile's one-shot setup service runs this migration without contacting hosted
models. Model policy or availability can therefore fail an actual embedding request without
blocking the database, MCP server, or worker from starting.

The baseline keeps one C-SPANN projection instead of duplicate ANN indexes on every source table.
Large embeddings live in their own CockroachDB column family, so text and scope scans do not read
vectors they never use. Composite parent and scope foreign keys let child row security use its
inverted scope index without running one parent visibility query for every row.

## Measured local baseline

The August 2026 Lambda emulator run loaded 87 public AIZK documents as 345 chunks. Ten warm `find`
calls measured 1.70 seconds at p50 and 2.64 seconds at p95 after the row security optimization.
Direct scoped C-SPANN execution took 3 to 7 milliseconds. The full graph plan still measured 8.29
seconds at p95 on a synthetic 1,000 chunk corpus, so the deep composed Find plan remains the main
CockroachDB performance limit. These numbers describe one local node and the public demonstration
shape. They are not CockroachDB Cloud latency claims.

The managed MCP connection uses `https://cockroachlabs.cloud/mcp` with the cluster ID header. The
hackathon queue steward uses a service account with Cluster Operator limited to this cluster, plus
a local seven-tool read-only allowlist. Interactive OAuth remains useful for one-time development
inspection. Managed MCP is an operator surface, not part of the user request path and not a
replacement for the application SQL roles. The self-contained service key and steward procedure is
in [`hackathon/operator/README.md`](../../../hackathon/operator/README.md).

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
