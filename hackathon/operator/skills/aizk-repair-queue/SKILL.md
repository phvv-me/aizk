---
name: aizk-repair-queue
description: Diagnose AIZK queue and file conversion failures through CockroachDB Cloud Managed MCP, apply the official CockroachDB operational skills, and guide a bounded retry through AIZK's typed recovery commands. Use for retained queue failures, failed file conversion, stale worker leases, repeated processing errors, or a pre-demo health review.
---

# AIZK Queue Steward

Treat a red queue count as a symptom. Separate a current AIZK failure from recovered history,
permanently unreadable input, a live worker lease, and a CockroachDB background job problem before
recommending any action.

## Load the operational guidance

Read these sibling skills before inspecting the deployment.

- `../reviewing-cluster-health/SKILL.md`
- `../monitoring-background-jobs/SKILL.md`

Use the Basic tier path because the demonstration runs on CockroachDB Cloud serverless. Keep every
Managed MCP call read only.

## Inspect through Managed MCP

1. Call `get_cluster` and confirm the expected cluster is available.
2. Call `select_query` against the configured AIZK database for only the bounded doctor summary. Do
   not select the complete report because it contains operator detail that the hosted model does
   not need.

   ```sql
   SELECT updated_at,
          report->>'generated_at' AS generated_at,
          report->>'healthy' AS healthy,
          report->'summary' AS summary
   FROM public.operator_snapshot
   WHERE key = 'doctor'
   LIMIT 1
   ```

3. Call `select_query` for current queue counts. Never select a payload or raw error message.

   ```sql
   SELECT status, entrypoint, count(*) AS tasks,
          min(created_at) AS oldest, max(updated_at) AS newest
   FROM public.queue_task
   GROUP BY status, entrypoint
   ORDER BY status, entrypoint
   LIMIT 100
   ```

4. In the same steward invocation, use the fixed direct SQL reader to aggregate failed, paused, and
   unusually long jobs from `SHOW JOBS`. The reader uses a login with only `VIEWJOB` and returns
   counts without job IDs, descriptions, errors, or SQL text. Managed MCP rejects this statement,
   so never ask Managed MCP to run it. If the direct reader is unavailable, keep job health unknown
   rather than turning missing visibility into zero.
5. Leave `show_running_queries` and `explain_query` outside the unattended collector. An operator
   may use them manually after the typed verdict identifies a live SQL or Find performance issue.

If Managed MCP authentication fails, stop the inspection and report that evidence. Never reuse,
copy, print, or persist an interactive OAuth token. Prefer a narrowly scoped service account API
key for unattended runs.

## Classify before repair

Apply these decisions in order.

- Treat recent exception history as context, not as a current failure.
- Leave `unreadable` conversions alone. The same bytes would repeat the same format verdict.
- Leave queued and fresh picked work alone.
- Inspect logs before touching a long running lease that still heartbeats.
- Confirm the owning worker is gone before treating a stale picked lease as abandoned.
- Retry a failed conversion only after its stored error points to a repaired transient cause.
- Escalate row security, migration, C-SPANN, schema, constraint, and persistent input failures.
- Never infer that an AIZK queue failure means CockroachDB itself failed.

Use DeepSeek only to classify the bounded sanitized evidence. Do not send source text, file names,
queue payloads, raw exception messages, credentials, connection strings, or organization data to
the model.

## Keep repair bounded

Managed MCP stays read only. Never call its create or insert tools for queue recovery. Never update
`queue_task` directly, and never pause, resume, or cancel a CockroachDB job automatically.

After explicit operator approval, use one AIZK domain command with a limit no larger than ten.

```bash
uv run --no-sync aizk admin queue retry conversion --limit 10
uv run --no-sync aizk admin queue retry graph --limit 10
uv run --no-sync aizk admin queue retry profile --limit 10
```

Choose only the command matching the diagnosed entrypoint. Run the Managed MCP inspection again
afterward. A retry count is not evidence of recovery. The queue must drain and the affected artifact
or projection must reach its expected durable state.

## Produce an auditable verdict

Return one compact report containing the cluster state, current queue failures, aggregate
CockroachDB job health, diagnosis, evidence used, proposed action, approval requirement, action
result, and follow-up verification. Name the Managed MCP tools, direct SQL statement, and official
skills actually used so the integration is visible in the hackathon evidence.

Never claim success when Managed MCP was unavailable or when follow-up verification did not run.
