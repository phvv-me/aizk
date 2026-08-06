---
title: "Observability"
description: "Logs, durable usage, the health overview, and diagnosing a stuck queue."
---

Two separate things answer two separate questions here. Telemetry explains what happened and why,
and it expires. The `usage_event` ledger records what work was done and it does not. This page
covers the ledger and the commands you actually run when something is stuck.
[Telemetry](/docs/dev/run/telemetry/) covers the traces, metrics and logs. Both assume the
service list from [Deployment topology](/docs/dev/run/topology/).

```text
  signals ─▶ containers ─▶ alloy ─▶ loki, tempo, victoriametrics ─▶ grafana (expires)
                │
  usage ────────┴─▶ PostgreSQL ─▶ usage_event (durable, never expires)
```

## Starting the stack

`--profile observability` starts the collector, the three stores and Grafana.
`observability-init` runs first and once, as root with only `CHOWN`, `DAC_OVERRIDE` and `FOWNER`,
to create each store's directory for the unprivileged user that owns it. This step is needed even
for named volumes, because dropping all capabilities leaves those users unable to fix a
root-owned directory.

```sh
AIZK_GRAFANA_ADMIN_PASSWORD=
AIZK_DOCKER_GID="$(stat -c %g /var/run/docker.sock)"
docker compose --profile observability --env-file .env -f src/deploy/docker-compose.yml up -d
```

Grafana on `127.0.0.1:3003` is the only host port anything in the Compose file publishes. Reach
it locally, forward it over SSH, or use the gated console at `admin.phvv.me/grafana`.

:::caution[Keep the observability stack off the network]
Never expose Grafana, Loki, Tempo, VictoriaMetrics, Alloy or the Docker socket. Alloy reads the
socket read-only, which still means broad visibility into every container's metadata and logs, so
treat it as host infrastructure rather than an application.
:::

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

- [Telemetry](/docs/dev/run/telemetry/) follows one query across every service it touched.
- [The job system](/docs/dev/passes/jobs/) explains what the queue is running.
- [Grounding and consolidation](/docs/dev/write/consolidation/) decodes the rejection reasons.
- [Upgrades](/docs/dev/run/upgrades/) covers the health check's place in a deployment.
- [The release gate](/docs/dev/run/release-gate/) lists what must be green before traffic.

</div>
