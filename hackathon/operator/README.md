# AIZK queue steward

The queue steward diagnoses retained AIZK queue, file conversion, and CockroachDB background job
failures in one invocation. A fixed collector uses CockroachDB Cloud Managed MCP for cluster and
AIZK state, then runs one direct read-only `SHOW JOBS` aggregate for the operational state Managed
MCP does not expose. DeepSeek V4 Flash applies the official CockroachDB Agent Skills once to the
combined sanitized evidence. The steward returns a typed recommendation and never performs a
repair by itself.

## Create the service key

1. Open [CockroachDB Cloud Access Management](https://cockroachlabs.cloud/access) and select the
   organization that owns the AIZK cluster.
2. Open **Service Accounts**, choose **Create**, and name the account `aizk-queue-steward`.
3. Create the account and copy the generated secret key before closing the dialog.
4. Edit the new service account roles.
5. Grant **Cluster Operator** only on the AIZK cluster. Do not grant an organization-wide role.
6. If no key was created with the account, open its details, choose **Create API Key**, and copy the
   secret key once.

CockroachDB requires Cluster Operator or Cluster Admin for Managed MCP. Cluster Operator at the one
cluster scope is the smaller valid grant. The local runner makes only fixed `get_cluster` and
`select_query` calls. A separate SQL login needs only `VIEWJOB` for the fixed job aggregate. The
model receives their normalized result and no database tools.

Create the SQL login through ccloud and grant its only system privilege through an administrative
SQL connection.

```sh
docker compose -f src/deploy/cockroachdb/docker-compose.ccloud.yml \
  run --rm ccloud cluster user create aizk-cockroachdb aizk_job_monitor
```

```sql
GRANT SYSTEM VIEWJOB TO aizk_job_monitor
```

Do not grant `CONTROLJOB`. The monitor may view cluster-wide jobs but cannot pause, resume, cancel,
or modify them.

Copy the operator environment template from the AIZK package root.

```sh
cp hackathon/operator/.env.example hackathon/operator/.env
```

Put the key in the ignored `hackathon/operator/.env` file. The cluster ID is optional when the
service account can access exactly one cluster.

```dotenv
CRDB_SERVICE_API_KEY=replace-with-the-secret-key
AIZK_COCKROACH_CLUSTER_ID=replace-with-the-cluster-uuid
AIZK_COCKROACH_DATABASE=replace-with-the-database-name
AIZK_COCKROACH_JOB_DATABASE_URL=replace-with-the-monitor-connection-url
AIZK_DEMO_OPENROUTER_API_KEY=replace-with-the-demo-key
```

The runner also accepts the key from the monorepo root `.env`, so an existing
`CRDB_SERVICE_API_KEY` does not need to be copied. The cluster UUID appears in the CockroachDB Cloud
cluster overview URL. The local Git ignore rule covers both environment files. Never commit the
service key or include it in screenshots. Delete the key from CockroachDB Cloud after judging if
the operator is no longer needed.

## Run the diagnosis

From the repository root, run the following command after the development bootstrap.

```sh
uv run --no-sync python hackathon/operator/steward.py diagnose
```

One invocation gathers all evidence, makes one model call, applies one deterministic policy
decision, and returns one verdict. It reads only aggregate queue rows, a bounded doctor summary,
and aggregate job states. It never reads job descriptions, the complete operator report, payloads,
file names, or stored errors. It names every Managed MCP tool, SQL statement, and official skill it
used. Any proposed action still requires explicit operator approval.

If the direct SQL credential is absent, job health remains explicitly unavailable rather than
being reported as zero.

Interactive OAuth remains available for a one-time local check.

```sh
uv run --no-sync python hackathon/operator/steward.py diagnose --oauth=true
```

The service key is the repeatable demonstration path because it does not depend on a daily browser
authorization refresh.

## Hackathon integration

This workflow makes two approved CockroachDB tools functional parts of the project.

- Managed MCP supplies live cluster and bounded queue evidence to the deterministic collector.
- CockroachDB Agent Skills supply the operational procedure and safety rules used to classify that
  evidence.

Distributed Vector Indexing remains part of every semantic Find request. The ccloud CLI remains the
reproducible cluster and SQL identity management path. AWS Lambda runs the application and durable
worker, while private S3 stores original uploaded files.

The integration is not an initialized placeholder. Removing Managed MCP leaves the steward without
live evidence. Removing the Agent Skills leaves it without the CockroachDB diagnostic workflow and
safety boundary. The model never chooses SQL or receives MCP tools. Removing CockroachDB removes
the queue, memory, vector index, and evidence being inspected.

The two official skills were installed from
[`cockroachlabs/cockroachdb-skills`](https://github.com/cockroachlabs/cockroachdb-skills) at commit
`e14e86d23ce8ee2e7e40a34ce2944c2502b6eadd`. That repository and AIZK both use Apache License 2.0.
