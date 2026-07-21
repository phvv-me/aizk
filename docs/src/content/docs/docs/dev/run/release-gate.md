---
title: "The release gate"
description: "The checks that must pass before a deployment is considered safe."
---

This is the list to walk before a deployment takes real traffic. It assumes the threat model on
[The security model](/docs/dev/run/security/) and the commands from
[Observability](/docs/dev/run/observability/). Every item here was checked against the code
rather than inherited from an earlier document.

```text
  code ──▶ config ──▶ identity ──▶ data ──▶ disk ──▶ edge
    │        │           │           │        │        │
    └─ CI    └─ preflight└─ Logto    └─ RLS   └─ backup└─ Cloudflare
       green    gates       policy      clean    tested   rules
```

## Code

- `chefe run lint`, `chefe run lint-imports`, `chefe run typecheck` and `chefe run test` are all
  green on the commit being deployed. CI runs the same four against a real VectorChord database.
- `tests/store/test_rls.py::test_chunk_write_requires_the_parent_document_scope` passes. This is
  the cross-tenant child-write regression and it is the one test that proves the foreign-key
  loophole is closed.
- The docs build passes, which includes the page gate in `docs/scripts/check-pages.mjs`.

## Configuration

- Grafana is the only host-published port and it binds `127.0.0.1`. Nothing else in
  `src/deploy/docker-compose.yml` publishes anything.
- `server` and `api` carry blank `AIZK_ADMIN_PASSWORD`, `AIZK_ADMIN_DATABASE_URL`,
  `AIZK_BACKUP_DATABASE_URL` and `AIZK_DATABASE_URL`, so neither can construct an owner
  connection.
- Every external image resolves to its pinned tag, and `db`, `objects`, `clamav` and `docling`
  also match their pinned digests.
- Database passwords are unique, random and absent from every tracked file.
- `AIZK_WEB_SESSION_SECRET` is at least 32 bytes and differs from the web, Management API and
  OAuth client secrets. `Settings.independent_session_secret` enforces this, so a failure here
  shows up as a container that will not start.

## Identity

- `public-check` exits zero. It runs `admin auth check-public` with `AIZK_REQUIRE_AUTH=1`, which
  constructs `Settings` and therefore fails when Logto, the public URLs or either OAuth client
  is missing or partial.
- `web-check` exits zero when the browser UI is enabled.
- `AIZK_WEB_PUBLIC_URL` and `AIZK_API_PUBLIC_URL` are both HTTPS. Settings validates the scheme
  but does not require the two to be equal, so confirm by hand that Caddy is routing them as one
  origin. This one is a convention, not a check.
- `aizk admin auth audit` reports clean, meaning the live Logto tenant matches the committed
  policy.
- `logto` connects as its own dedicated role owning only the `logto` database.
- A suspended account and an account without the `aizk-user` global role are both rejected by the
  browser path. `LogtoClient._screen_account` and `_screen_roles` are what do it.
- Organization management adds only an existing account by exact email and refuses to demote or
  remove the last administrator.
- The `frontend` image contains no database password, Logto secret or session secret. Every one
  of those is runtime environment on the container, never a build argument.

## Data

- Alembic is at head and `aizk admin database check-rls` prints `ok`. The same verification shows
  up as an empty `rls_violations` list in `aizk admin health`.
- Per-caller rate limiting and the MCP request size limits are active with their configured
  values.
- `SHOW data_checksums` returns `on`.
- The health report finishes inside its bounds and its real recall returns candidates with no
  `error`. The probe timeouts are 2 seconds per model endpoint and 3.5 for the recall.
- `aizk admin queue doctor` exits zero, meaning no current blockers.

## Disk and backups

- The device holding PostgreSQL has at least 20 percent free.
- SMART monitoring, temperature alerting and periodic TRIM are enabled on the host.
- A current aizk archive and a current Logto archive both exist off-host in encrypted storage.
  They are separate dumps and a deployment needs a matched pair.
- A matching generation of the SeaweedFS object data exists off-host. There is no automated job
  for this in the repository, so it is a manual step and it is easy to skip.
- A scratch restore has passed within the last month, all the way through PostgreSQL accepting
  the archive, the RLS check passing, Logto starting and an authenticated recall returning
  evidence.

## Edge

- Cloudflare rate and body-size rules protect `/authorize`, `/register`, `/token` and `/mcp`.
  Application middleware only sees tool calls after FastMCP builds a request context, so it does
  not cover those routes.
- Container egress is restricted so URI intake cannot reach loopback, link-local, private or
  metadata networks through DNS rebinding.

## Accepted gaps

:::caution[Two items are accepted gaps, not passes]
Record them as known and accepted rather than quietly skipping them.
:::

The dedicated PostgreSQL device is not LUKS encrypted, and the reference host has no TPM
available for unattended unlock. [PostgreSQL and storage](/docs/dev/run/postgres/) explains the
two honest designs. Until one is chosen, sensitive data on this deployment is protected by host
access control and not by encryption at rest.

Object-store backup is manual. PostgreSQL archives preserve every hash, derivative and piece of
artifact metadata, but not the original bytes, so a restore without a matching SeaweedFS copy
gives you a memory that knows about files it can no longer open.

## What changed from the old checklist

Two items in the previous version were wrong. There is no `pgrls lint` command here, and the real
check is `aizk admin database check-rls` backed by `ops.scoped_rls_violations`. And the browser
image was called static, which it is not, since `frontend` is an adapter-node server that renders
every page. Its real claim, that no secret is baked at build time, still holds.

## Next

<div class="not-content">

- [The security model](/docs/dev/run/security/) explains why each item is on this list.
- [Backups and recovery](/docs/dev/run/backups/) covers the restore drill in detail.
- [Upgrades](/docs/dev/run/upgrades/) covers where this gate sits in a deployment.
- [Observability](/docs/dev/run/observability/) has the commands the data section runs.

</div>
