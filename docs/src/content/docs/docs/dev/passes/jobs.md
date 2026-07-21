---
title: "The job system"
description: "PgQueuer, the job types, the priorities, and how a cron pass fans out across scopes."
---

Everything aizk does after a write has already returned happens through one durable queue. This
page assumes you know what a [scope set](/docs/dev/identity/scope-sets/) is and that maintenance
runs as the system user, which [Background work](/docs/dev/identity/background/) covers.

## One database, no broker

The queue is PgQueuer, which stores jobs in ordinary PostgreSQL tables and wakes workers with
`LISTEN` and `NOTIFY`. We picked it over Celery, taskiq, or anything backed by Redis or RabbitMQ
for one reason. A write and its follow-up job land in the same transaction, so there is no window
where a chunk exists but its projection job was lost, and there is no second datastore to back up,
secure and reason about. Everything already lives in one Postgres and the queue stays there too.

`src/aizk/background/queue.py` wraps it. `Queue` is an async context manager over one asyncpg
connection, `install_queue_schema()` creates PgQueuer's tables under an advisory lock and grants
the app role only the objects PgQueuer reports installing, and `run_worker()` in `schedule.py`
binds every job before calling `pg.run(batch_size=settings.queue_batch_size, ...)`, which defaults
to 64 with four times that many concurrent tasks.

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

Three jobs are enqueued by application code as work arrives.

| Job | Entrypoint | Priority | Concurrency |
|---|---|---|---|
| `ChunkProjectionJob` | `aizk_build_graph_chunk` | 50 | `graph_build_concurrency`, 4 |
| `DoclingConversionJob` | `aizk_convert_artifact` | 75 | `docling_concurrency`, 4 |
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
| `CommunitiesJob` | `0 4 * * 0` | `build_communities` behind the growth gate |
| `RaptorJob` | `30 4 * * 0` | `build_raptor` behind the growth gate |
| `ProfileRefreshJob` | `0 5 * * 0` | `refresh_profiles` |
| `InsightJob` | `0 7 * * 0` | `derive_insights` |

A `SystemScheduledJob` runs once with no scope fan-out, because its work is not tenant shaped.

| Job | Default cron | Body |
|---|---|---|
| `ChunkRecoveryJob` | `* * * * *` | requeue 512 held chunk failures, max 3 cycles each |
| `ArtifactIntegrityJob` | `0 6 * * *` | re-verify 100 originals older than 30 days |
| `BackupJob` | `0 2 * * *` | `scheduled_backup`, off unless `AIZK_BACKUP_ENABLED` |

## Names are derived, not typed twice

`ScheduledJob.__init_subclass__` computes everything from the class name. `CommunitiesJob` becomes
`name = "communities"`, `cron_entrypoint = "aizk_cron_communities"` and, for scoped jobs,
`entrypoint = "aizk_task_communities"`. The `expression` and `enabled` properties then read
`settings.communities_cron` and `settings.communities_enabled`. Adding a pass means subclassing,
implementing `execute`, and adding those two settings. Nothing registers it by hand, because
`ScheduledJob` is a patos `Registry` and `run_worker` iterates `ScheduledJob.implementations()`.

## Deduplication and holding

Every enqueue passes a `dedupe_key`. `install_queue_schema` creates a partial unique index over
that column restricted to the `queued`, `picked` and `failed` statuses, so a duplicate is rejected
while a job is live or held but the same key is admitted again once the earlier run succeeded.
`Queue.enqueue` catches PgQueuer's `DuplicateJobError` and returns `False` rather than raising,
which is what lets `ChunkDispatchJob` sweep every pending chunk each minute without ever
double-projecting one.

Keys are stable and boring. A chunk job uses `str(chunk.id)`, a conversion uses its content ID, a
usage event uses its capture key, and a fan-out uses the job name joined to its sorted scopes.

Failures are held rather than dropped. `QueueJob.bind` registers with `on_failure="hold"` and a
`DatabaseRetryEntrypointExecutor` capped at `max_attempts`, so a job retries five times and then
stays in the table with status `failed`. `Queue.requeue_failed` puts a bounded window of those back
in flight, filtering by entrypoint inside the SQL so one noisy job type cannot crowd out another,
and optionally capping how many terminal cycles a row may already have burned. That cap is why
`ChunkRecoveryJob` retries automatically at most `chunk_recovery_max_cycles` times, which is 3,
while an operator running the retry command may pass no cap at all.

## The loop

```text
     cron fires                         a write arrives
         |                                    |
  scope_roster (as owner)          enqueue chunk / conversion
         |                                    |
  one job per exact scope set                 |
         +-------------->  pgqueuer queue  <---+
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

- [Communities and RAPTOR](/docs/dev/passes/communities-raptor/) covers the clustering passes and the growth gate that holds them back.
- [Profiles, insights, decay](/docs/dev/passes/profiles-insights/) covers the per-entity summaries and the aging pass.
- [Promotion and sharing](/docs/dev/passes/promotion/) covers working memory graduating into the graph.
- [Observability](/docs/dev/run/observability/) covers watching the queue in production.

</div>
