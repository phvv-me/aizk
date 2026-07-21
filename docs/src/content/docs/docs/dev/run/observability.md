---
title: "Observability"
description: "Logs, durable usage, the health overview, and diagnosing a stuck queue."
---

Two separate things answer two separate questions here. Logs explain why something failed and
they expire. The `usage_event` ledger records what work was done and it does not. This page
covers both, plus the commands you actually run when something is stuck. It assumes the service
list from [Deployment topology](/docs/dev/run/topology/).

```text
  logs ──▶ containers ─▶ alloy ─▶ loki (720h, 30-day retention) ─▶ grafana
                │
  usage ────────┴─▶ PostgreSQL ─▶ usage_event (durable, never expires)
```

## The logging stack

`--profile observability` starts four services. `observability-init` runs first and once, as
root with only `CHOWN`, `DAC_OVERRIDE` and `FOWNER`, to create `/loki` for UID 10001, `/alloy`
for UID 473 and `/grafana` for UID 472. This step is needed even for named volumes, because
dropping all capabilities leaves those unprivileged users unable to fix a root-owned directory.

`alloy` discovers containers through `discovery.docker` filtered on the label
`com.docker.compose.project=aizk`, relabels each target with its container name and Compose
service, and pushes to Loki. It runs as `473:473` with the Docker socket mounted read-only, and
`AIZK_DOCKER_GID` adds only the supplemental host group it needs to open that socket.

`loki` uses a TSDB index on the local filesystem with the `aizk_logs_` prefix and a compactor
with `retention_period: 720h`, which is 30 days. `grafana` has one provisioned, noneditable Loki
data source named "AIZK logs", anonymous access off and sign-up off.

```sh
AIZK_GRAFANA_ADMIN_PASSWORD=
AIZK_DOCKER_GID="$(stat -c %g /var/run/docker.sock)"
docker compose --profile observability --env-file .env -f src/deploy/docker-compose.yml up -d
```

Grafana on `127.0.0.1:3003` is the only host port anything in the Compose file publishes. Reach
it locally or forward it over SSH.

:::caution[Keep the observability stack off the network]
Never expose Grafana, Loki, Alloy or the Docker socket. Alloy reads the socket read-only, which
still means broad visibility into every container's metadata and logs, so treat it as host
infrastructure rather than an application.
:::

Traces stay inside each Python process unless `AIZK_OTLP_ENDPOINT` names an OTLP over HTTP
collector, so span export is off until something like Tempo joins this profile.

## Durable usage

`UsageAccountingJob` in `src/aizk/usage.py` appends one row per successful operation to the
immutable `usage_event` table, through PgQueuer on the `aizk_usage_event` entrypoint. Enqueue is
transactional and the handler is idempotent, so a job PgQueuer reclaims after a late
acknowledgement is stored once.

Each event carries the authenticated actor, the exact target scope IDs, request bytes, response
bytes, the item count and the capture time. A multi-scope event is attributed to every target,
because each organization took part in it, which means actor totals are the nonduplicated view
and scope totals deliberately are not.

Storage reporting keeps two numbers apart on purpose. Per scope-set you get artifact revision
count and logical original bytes. Globally you get unique physical blobs, original bytes, stored
bytes and bytes saved by compression. A blob shared by two organizations counts twice logically
and once physically, and pretending otherwise would either overstate the disk or understate who
used it.

## The five second overview

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml exec -T worker aizk admin health
```

`ops.health()` fans out concurrently over the migration head, the RLS verifier, row counts, the
queue overview, per scope-set corpus progress, usage totals and the four model endpoints, then
runs one real recall. Endpoint probes time out at 2 seconds and the recall at 3.5, so the whole
report is bounded.

Run it in `worker`, never in `server`. The public process has no owner credential by design.

A healthy report has an up-to-date migration, no RLS violations, Logto identity mode, all four
endpoints reachable with `matched` true, no retained queue failures, processed chunks catching up
with stored chunks, and a `recall` block with candidates and no `error`.

## A stuck queue

The doctor is read only, exits nonzero when there are current blockers, and never changes state.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml exec -T worker \
  aizk admin queue doctor
```

Its JSON groups current retained failures by entrypoint and a safe error fingerprint, then
reports stale picked leases, long-running live leases, recent exception aggregates, durable failed
conversions, and conversions whose durable active state points at a job the queue has already
finished. That last class is the usual cause of a conversion that looks busy forever. Complete
counts stay separate from the bounded detail lists, and error messages are redacted by default
because an upstream exception can quote source text.

Defaults are 15 stale minutes, 60 long-running minutes, a 24 hour history window and 50 detail
rows. Widen them when the workload is unusual, and opt into messages only as a trusted operator.

```sh
aizk admin queue doctor --stale-minutes 30 --history-hours 72 --limit 100
aizk admin queue doctor --show-error-messages
```

Fix the reported cause before retrying, then requeue the class you repaired.

```sh
aizk admin queue retry conversion --limit 100
aizk admin queue retry graph --limit 100
aizk admin queue retry profile --limit 100
```

Retrying only requeues retained failures. It does not repair an invalid source, an ontology
mismatch, abandoned durable state or a converter bug.

## Read-only extraction diagnosis

To see why one chunk produced nothing, run extraction and grounding over it without writing.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml run --rm --no-deps worker \
  admin graph diagnose-extraction 019f6bf4-ec29-72c5-93d2-59f791ae42d0
```

This needs owner access to read an arbitrary stored chunk, so it runs in `worker`. It does not
mark the chunk processed and writes no graph rows. The JSON holds the proposed extraction, a
rejection reason per fact, and the grounded subset with acceptance counts. `missing_quote`,
`unsupported_quote`, `unresolved_endpoint`, `self_relation` and `generic_relation` are deliberate
evidence failures rather than transport errors, and
[Grounding and consolidation](/docs/dev/write/consolidation/) explains each one.

## Next

<div class="not-content">

- [The job system](/docs/dev/passes/jobs/) explains what the queue is running.
- [Grounding and consolidation](/docs/dev/write/consolidation/) decodes the rejection reasons.
- [Upgrades](/docs/dev/run/upgrades/) covers the health check's place in a deployment.
- [The release gate](/docs/dev/run/release-gate/) lists what must be green before traffic.

</div>
