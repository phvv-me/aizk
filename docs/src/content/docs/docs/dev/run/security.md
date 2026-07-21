---
title: "The security model"
description: "What a deployment protects, what it trusts, and where it fails closed."
---

This page states what a production deployment protects, what it deliberately trusts, and where the
guarantees stop. It assumes the single-host Compose deployment from
[Deployment topology](/docs/dev/run/topology/) and the scope model from
[Scope sets in depth](/docs/dev/identity/scope-sets/).

## What is protected

aizk holds private notes, shared sources, embeddings, the graph, temporal history, sessions and
backups. Four goals follow from that.

- One caller never reads or alters another's private memory, and must stand in every scope on a
  shared row.
- Public request handling never holds a credential that can bypass row security.
- Secrets, passwords and backups stay out of logs and process arguments.
- Stored text stays untrusted data, and a stolen disk is never the only path to the memory.

## Trust boundaries

Logto owns users, organizations, memberships, roles, login and consent. aizk owns only the
deterministic mapping from a verified Logto identifier to a PostgreSQL UUID5. Each stage below
narrows authority, and the last filter runs under a role that cannot turn it off.

```text
  agent, MCP client
   └▶ Cloudflare tunnel, the only ingress
       └▶ FastMCP OAuth proxy
           └▶ verify JWT: issuer, audience, signature, control scope
               └▶ resolve User from Logto orgs and roles
                   └▶ per-caller token bucket
                       └▶ recall, remember, share, artifact
                           └▶ bind caller scopes into app.scopes
                               └▶ forced RLS inside PostgreSQL
                                    every scope present ─▶ tenant rows
                                    missing a scope    ─▶ row filtered out
```

The MCP server checks signature, issuer, expiry, the required `control` scope and the exact
resource audience, then resolves organization standing through a short coalesced Management API
cache. A failed refresh closes shared authority rather than keeping it.

The browser path skips that cache. Every protected load re-reads the account and its global roles,
so a deleted or suspended account, or one lacking the `aizk-user` role, is rejected on the next
decision by `LogtoClient._screen_account` and `_screen_roles`.

PostgreSQL is the final authority. `aizk_app` is neither a superuser nor a `BYPASSRLS` role and
every tenant table forces row security. Owners normally bypass RLS, which is why the public server
never gets the owner password. The `chunk` table inherits read visibility from its
document, and its write policy also requires matching scope sets, closing the loophole where a
guessed document ID could carry a child row. [Row level security](/docs/dev/store/rls/) has the
policies.

## Process privilege separation

One image, several services, two database roles.

```text
  process         database role            reaches
  ───────         ─────────────────        ─────────────────────────────────
  server (MCP)    aizk_app                 tenant tables, every row checked
  api (browser)   aizk_app                 tenant tables, every row checked
  worker          aizk_app + aizk_admin    tenant tables, owner bypasses RLS
  setup           aizk_admin               tenant tables, migrations only
  logto           logto                    the logto database only
```

`setup` holds the owner credential only while migrating. `server` and `api` get explicitly blank
`AIZK_ADMIN_PASSWORD`, `AIZK_ADMIN_DATABASE_URL` and `AIZK_BACKUP_DATABASE_URL`, so no owner edge
exists for them. `worker` keeps both roles because scope discovery, backups and schema maintenance
need the owner. `frontend` holds only the Logto web app and the session secret, while `caddy` and
the model services hold none.

Every long-lived aizk container runs as UID 10001 with a read-only root filesystem, no
capabilities, `no-new-privileges` and a bounded process count. This bounds the blast radius of a
compromised request path. It does nothing against host root compromise, Docker daemon compromise,
a malicious image, or code executed inside `worker`.

## Network exposure

The Cloudflare tunnel is the only ingress and it is outbound. Grafana is the single published host
port and it binds `127.0.0.1`. Caddy refuses every `/api` path except capability upload `PUT`s,
described on [The HTTP API](/docs/dev/interfaces/http-api/).

PostgreSQL TLS is off inside the Compose network, which is acceptable only while the database is
loopback bound and every client is on the same host. ClamAV's TCP protocol has no authentication
at all and must never leave that network. A future remote database needs `sslmode=verify-full`.

Public organizations affect read standing only. A write needs current Logto membership with
`write:memory`, and then forced row security applies the same test again inside PostgreSQL.
Cloudflare should also rate-limit and size-limit `/authorize`, `/register`, `/token` and `/mcp`,
because application middleware only sees tool calls after FastMCP has built a request context.

## Work limits

Each resolved user gets an independent five-second token bucket at five calls per second by
default, and a process holds at most 4096 buckets. It is abuse control, not quota accounting.

| Input | Default |
|---|---|
| Recall query | 16,384 characters |
| Recall evidence budget | 16,384 tokens |
| Remembered source | 5,000,000 characters |
| Source URI | 4,096 characters |
| Scope names per call | 32 |
| Shared documents per call | 100 |

## Artifact intake

Every byte is treated as hostile. URI intake permits only uncredentialed HTTPS whose DNS answers
are all public, revalidating on each redirect, with bounds on time, size and redirect count.
Production should still restrict container egress against DNS rebinding. ClamAV scans before
persistence and fails closed, so malware, a downed daemon, a malformed reply or a size violation
all reject the intake.

:::caution[The real upload ceiling is 10 MiB, not 96]
`AIZK_OBJECT_STORE_UPLOAD_BYTE_LIMIT` defaults to 96 MiB, but ClamAV ships with `MaxFileSize`,
`MaxScanSize` and `StreamMaxLength` all at `10M`, and scanning is fail-closed. Raising one without
the other only moves where the rejection happens.
:::

Stored objects use opaque random keys with no public bucket access. Reading bytes is a separate
request that re-resolves the caller, applies row security, requires the exact revision and
verifies the digest, so a guess grants nothing.
[Artifacts](/docs/dev/write/artifacts/) covers the pipeline.

## Stored text, secrets and supply chain

An authorized source can still be malicious. aizk treats source text as data during extraction and
returns recall as one evidence string, and the client skill sets that boundary once. That reduces
accidental instruction following but cannot guarantee model behavior, so client agents must never
let recalled text outrank system or user instructions. Write authority also stays a real
privilege, since an authorized writer can poison their own scope.

Rotating the OAuth client secret is a full session reset, because FastMCP encrypts its `/oauth`
state with keys derived from it. [Upgrades](/docs/dev/run/upgrades/) covers that.

:::caution[Choosing a model repository is choosing executable code]
The image installs from the committed `uv.lock` with frozen resolution, but vLLM runs checkpoints
with `trust-remote-code`. Only trusted repositories and pinned revisions belong in production, and
the Hugging Face cache must not be writable by untrusted users.
:::

## Next

<div class="not-content">

- [The release gate](/docs/dev/run/release-gate/) turns all of this into a pass or fail list.
- [Row level security](/docs/dev/store/rls/) has the policies and how they are verified.
- [The Logto boundary](/docs/dev/identity/logto/) explains what identity aizk does not own.
- [Backups and recovery](/docs/dev/run/backups/) covers backup confidentiality.

</div>
