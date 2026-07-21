---
title: "Background work"
description: "How a job that has no caller still runs under an exact scope set."
---

This page assumes you know that every row carries a sorted nonempty `uuid[]` and that reads are
decided by array containment, which [Scope sets in depth](/docs/dev/identity/scope-sets/) covers.
How the passes themselves are scheduled and drained is on
[The job system](/docs/dev/passes/jobs/).

## The problem a cron has

An MCP call arrives with a token, so its scopes are decided before the first statement runs. A
cron entry arrives with nothing. Community building, decay, RAPTOR and profile refresh all need to
read claims and write projections, and PostgreSQL will not let them read anything without a
standing to check against.

:::caution[Keep the boundary in the database]
The wrong answer is to run maintenance as the owner role and filter in Python. That moves the
boundary out of the database, which is the one thing the whole design refuses to do. The right
answer is to give the job an exact scope set before it starts.
:::

## The roster

`scope_roster()` in `src/aizk/background/schedule.py` is the one place that reads past row
security, and it reads almost nothing.

```python
async with User.system().owner as db:
    rows = await db.exec(Document.scope_sets(SessionItem, Artifact))
    keys = {frozenset(scopes) for (scopes,) in rows if scopes}
    return sorted(keys, key=lambda scopes: sorted(scopes))
```

`Scoped.scope_sets` is a `UNION` of `select(cls.scopes)` across the model and its peers, so the
roster is every distinct scope array that any document, session item or artifact actually stores.
It never returns a row, only the arrays, and `user.owner` refuses to open at all unless the caller
is `settings.system_user_id`.

## The fan-out

`fan_out(job)` turns that roster into durable queue items, one per set.

```text
            cron fires aizk_cron_communities
                        │
                        ▼
                  scope_roster()
            ┌───────────┼───────────────┐
            ▼           ▼               ▼
      Job {me}   Job {book club}   Job {book club, uni}
            └───────────┼───────────────┘
                        ▼
      execute(scopes) under User.system(scopes)
```

Each item is enqueued with a dedupe key of `f"{job.name}:{','.join(map(str, sorted(key)))}"`, so a
cron that fires while the previous run is still queued adds nothing. `Queue.enqueue` catches
`DuplicateJobError` and returns `False`, and the counted total is logged.

Every scheduled job runs at `JobPriority.maintenance`, which is **10**. Chunk work is 50 and
artifact work is 75, so anything a person is waiting on outranks the whole maintenance tier.
`ScopedScheduledJob.concurrency_limit` is 1, so one scope set's pass never runs twice at once.

Not everything fans out. `SystemScheduledJob` covers work that has no tenant at all, and
`ChunkRecoveryJob`, `ArtifactIntegrityJob` and `BackupJob` are its implementations. Those run once
per cron tick with no scope payload.

## The identity the body runs under

`ScopedScheduledJob.handle` converts the payload back into a `frozenset` and calls
`execute(scopes)`. Inside, work opens `User.system(scopes)`, which puts that exact set into both
the read and the write side of the caller's `ScopeTable`. An empty argument falls back to
`{settings.system_user_id}` rather than to nothing, so a misconfigured job sees the system's own
private scope instead of somebody else's memory.

From there the pass is an ordinary caller. It goes through the app role, the same `scope_read` and
`scope_insert` policies apply, and a bug in the pass body cannot widen what it touches.

## Why exact sets and not users

This is the part worth slowing down on, because partitioning by user looks simpler and is wrong.

Suppose Ada is in her private scope and also in the organization Book Club, and some of her memory
sits in the intersection `{book club, uni}`. Partition by user and one community pass runs with a
read set covering all three, which means the graph it clusters mixes claims from three different
visibilities. Now the pass has to write a community row, and that row needs one scope array. Any
array it picks is wrong. Pick `{ada}` and the row is derived from Book Club content but readable
privately, which leaks. Pick `{book club, uni}` and most of its members become invisible to the
people who can see the row, which is useless.

Partition by exact scope set and the problem disappears. The read set and the written array are
the same value, so the projection is closed. Everything the pass could read is exactly what its
output is readable by. That is also why bodies can use plain equality, as in
`Fact.Claim.scopes == sorted(scopes)` inside `recorded_fact_count`, to count precisely the claims
belonging to their own partition rather than everything visible to them.

The cost is real and worth naming. A deployment with many distinct intersections gets many small
jobs rather than a few large ones, and a scope set with two documents in it still pays a full pass.
`run_if_grown` softens that by checking a `Watermark` and skipping a rebuild when the recorded
fact count has not grown past a threshold since the last run.

## Next

<div class="not-content">

- [The job system](/docs/dev/passes/jobs/) covers the queue, the schedule and retries.
- [Communities and RAPTOR](/docs/dev/passes/communities-raptor/) is the biggest consumer of this.
- [Scope sets in depth](/docs/dev/identity/scope-sets/) has the policy these jobs run under.

</div>
