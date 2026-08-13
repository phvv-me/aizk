---
title: "The job system"
description: "The two durable queue adapters, job types, priorities, recovery and scoped scheduling."
---

Everything aizk does after a write has already returned happens through one durable queue. This
page assumes you know what a [scope set](/docs/dev/identity/scope-sets/) is and that maintenance
runs as the system user, which [Background work](/docs/dev/identity/background/) covers.

## One database, two queue adapters

The queue always lives in the selected SQL backend. AIZK needs no Redis, RabbitMQ or external
workflow service. The PostgreSQL profile uses PgQueuer and its `LISTEN` and `NOTIFY` worker. The
CockroachDB profile uses AIZK queue and event tables with a portable worker that claims rows with
transactional locks. Both retain failures and deduplicate active work.

`src/aizk/background/queue.py` owns both adapters behind `Queue`. On PostgreSQL,
`install_queue_schema()` creates PgQueuer objects under an advisory lock. On CockroachDB, the main
migration creates `queue_task` and `queue_event`. `run_worker()` selects PgQueuer or
`PortableWorker` from `AIZK_DATABASE_BACKEND`. The Lambda worker uses a bounded batch of eight,
while the self-hosted worker defaults to a larger continuous drain.

## What a job declares

`QueueJob` is the base every job type subclasses. A job is five class variables and one `handle`
method.

```python
class QueueJob[PayloadT: QueuePayload](abc.ABC):
    entrypoint: ClassVar[str]
    payload_type: ClassVar[type[QueuePayload]]
    priority: ClassVar[int] = 0
    concurrency_limit: ClassVar[int] = 0
    max_attempts: ClassVar[int] = 5
```

The payload is a Pydantic `QueuePayload` that serializes to JSON bytes, so a malformed row fails
validation at decode rather than deep inside a handler. A `concurrency_limit` of zero means
unbounded. Priority comes from `JobPriority` in `background/enum.py` and larger runs first.

| Priority | Value | Used by |
|---|---|---|
| `maintenance` | 10 | every scheduled pass, without exception |
| `chunk` | 50 | `ChunkProjectionJob` |
| `artifact` | 75 | `DoclingConversionJob` |

Artifact conversion outranks chunk projection because a PDF that has not been converted yet
produces no chunks at all, and both outrank maintenance because somebody is waiting on the first
two and nobody is waiting on a nightly rebuild.

## The inventory

Four jobs are enqueued by application code as work arrives. `MarkdownReindexJob` replays chunking
and embedding from stored Markdown without Docling, so it shares the conversion priority and limit.

| Job | Entrypoint | Priority | Concurrency |
|---|---|---|---|
| `ChunkProjectionJob` | `aizk_build_graph_chunk` | 50 | `graph_build_concurrency`, 4 |
| `DoclingConversionJob` | `aizk_convert_artifact` | 75 | `docling_concurrency`, 4 |
| `MarkdownReindexJob` | `aizk_reindex_artifact` | 75 | `docling_concurrency`, 4 |
| `UsageAccountingJob` | `aizk_usage_event` | 0 | unbounded |

The rest are scheduled. A `ScopedScheduledJob` fans out into one queue item per scope set, and all
of them run at priority 10 with `concurrency_limit = 1`.

| Job | Default cron | Body |
|---|---|---|
| `ArtifactDispatchJob` | `* * * * *` | re-dispatch originals a crashed handoff left pending |
| `ChunkDispatchJob` | `* * * * *` | `enqueue_pending`, up to 512 chunks |
| `ProfileProjectionJob` | `* * * * *` | `refresh_dirty_profiles` |
| `SessionPromoteJob` | `*/15 * * * *` | `promote_sessions` |
| `DecayJob` | `0 3 * * *` | `decay`, half life 90 days |
| `DedupJob` | `30 3 * * *` | `dedup_entities` |
| `CommunitiesJob` | `0 4 * * 0` | `build_communities` behind the growth gate, then queues RAPTOR |
| `RaptorJob` | off by default | `build_raptor`, queued by the community pass that feeds it |
| `ProfileRefreshJob` | `0 5 * * 0` | `refresh_profiles` |
| `InsightJob` | `0 7 * * 0` | `derive_insights` |

A `SystemScheduledJob` runs once with no scope fan-out, because its work is not tenant shaped.

| Job | Default cron | Body |
|---|---|---|
| `ChunkRecoveryJob` | `* * * * *` | requeue 512 held chunk failures, max 3 cycles each |
| `ArtifactIntegrityJob` | `0 6 * * *` | re-verify 100 originals older than 30 days |
| `CleanupJob` | `0 1 * * *` | trim queue history past 7 days, then `VACUUM (ANALYZE)` |
| `BackupJob` | `0 2 * * *` | `scheduled_backup`, off unless `AIZK_BACKUP_ENABLED` |

## PostgreSQL queue history must be trimmed

PgQueuer writes one `pgqueuer_log` row per finished job and nothing reads it for correctness, so a
busy deployment turns it into the biggest table it owns. `CleanupJob.prune_pgqueuer_log` deletes
past `cleanup_log_retention_days`, 7, in batches of `cleanup_log_delete_batch`, 10,000, looping
until a short batch ends the drain. One nightly pass clears the whole backlog, and PgQueuer indexes
`created`, so every batch is an index scan.

The `VACUUM (ANALYZE)` that follows never takes an exclusive lock and never hands disk back either.
[PostgreSQL and storage](/docs/dev/run/postgres/) has the one time recovery for a table that grew
before the job first ran.

## Names are derived, not typed twice

`ScheduledJob.__init_subclass__` computes everything from the class name. `CommunitiesJob` becomes
`name = "communities"`, `cron_entrypoint = "aizk_cron_communities"` and, for scoped jobs,
`entrypoint = "aizk_task_communities"`. The `expression` and `enabled` properties then read
`settings.communities_cron` and `settings.communities_enabled`. Adding a pass means subclassing,
implementing `execute`, and adding those two settings. Nothing registers it by hand, because
`ScheduledJob` is a patos `Registry` and `run_worker` iterates `ScheduledJob.implementations()`.

## Deduplication and holding

Every enqueue passes a `dedupe_key`. Both schemas reject a duplicate while a job is queued,
picked or failed, then admit the same key after the earlier run succeeds. `Queue.enqueue` returns
`False` for the conflict rather than raising. This lets a recovery sweep revisit pending rows
without double-projecting one.

Keys are stable and boring. A chunk job uses `str(chunk.id)`, a conversion uses its content ID, a
usage event uses its capture key, and a fan-out uses the job name joined to its sorted scopes.

Failures are held rather than dropped. PgQueuer uses its database retry executor. The portable
worker records attempts and the terminal error in AIZK tables. `Queue.requeue_failed` puts a
bounded window back in flight and filters by entrypoint in SQL, so one noisy job type cannot hide
another. Automatic chunk recovery caps terminal cycles, while an explicit operator retry can omit
that cap.

## The loop

```text
     cron fires                         a write arrives
         |                                    |
  scope_roster (as owner)          enqueue chunk / conversion
         |                                    |
  one job per exact scope set                 |
         +-------------->   durable queue   <---+
                        (partial unique dedupe_key)
                                 |
                worker picks the highest-priority ready job
                                 |
                     handle, decoded to its payload type
                        |                          |
                     success                  attempts left?
                (dedupe key freed)         yes --> back to pick
                                           no  --> failed, row retained
                                                        |
                                            requeue_failed (bounded) --> queue
```

`scope_roster()` is the part worth pausing on. It runs under the database owner so row security
does not hide other tenants, unions the distinct `scopes` arrays of `document`, `session_item` and
`artifact`, and returns each exact set it finds. A cron tick therefore produces one job per set
that actually holds memory, and each of those jobs then runs entirely inside
`User.system(scopes)`, so the pass sees exactly the rows a member of that scope set would see.

:::caution[The fan-out reads as owner, the jobs do not]
Only `scope_roster` runs with row security off, to see every tenant's scope sets. Each job it spawns
runs inside `User.system(scopes)` and sees just that one set. Keep new maintenance work inside that
per-scope session and never widen the owner query into a job body.
:::

## Next

<div class="not-content">

- [Communities](/docs/dev/passes/communities/) covers the clustering pass and the growth gate that holds it back.
- [Profiles, insights, decay](/docs/dev/passes/profiles-insights/) covers the per-entity summaries and the aging pass.
- [Promotion and sharing](/docs/dev/passes/promotion/) covers working memory graduating into the graph.
- [Observability](/docs/dev/run/observability/) covers watching the queue in production.

</div>
