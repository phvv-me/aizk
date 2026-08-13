---
title: "The MCP server"
description: "The tool surface, the middleware, and the OAuth proxy that fronts it."
---

This page assumes you know how a caller is verified, which
[The Logto boundary](/docs/dev/identity/logto/) covers, and what the tools do from a user's point
of view, which [MCP tools](/docs/user/reference/tools/) covers. Here we look at
`src/aizk/mcp/` as code.

## One class, assembled once

`AizkMCP` in `src/aizk/mcp/server.py` subclasses `FastMCP`. It takes an `Auth`, a `ByteStore`, an
`UploadBox`, an `ArtifactIntake` and the `Settings`, and `aizk admin server mcp` builds exactly one
per process from `Runtime.assemble`.

Its constructor does four things in order. It calls `super().__init__(name, auth=auth.provider())`,
adds `IdentityMiddleware`, adds `CallerRateLimit`, then registers five tools and one resource
template.

The tools are not module-level functions. Each is built by a method that closes over
`self.settings`, so bounds like `mcp_recall_query_max_chars` and `mcp_remember_max_chars` are read
when the server is constructed rather than at import time. That is what lets a deployment change a
limit without a code change.

| Tool | Identified | What it does |
|---|---|---|
| `status` | yes | caller authority plus durable usage and processing health |
| `find` | no | visible evidence for one question, rendered as Markdown |
| `keep` | yes | store text, preserve a URI original, or mint an upload ticket |
| `report` | yes | send exact confusing or contradictory evidence to an operator-only scope |
| `share` | yes | copy visible documents into one authorized destination |

A user never hand-writes the MCP JSON. An agent harness reaches the five tools by name, like this.

```text
aizk.status(days=30)
aizk.find(query="why did we switch extractors?", budget=2048)
aizk.keep(text="reranker sidecar is back up", scopes=["Book Club"])
aizk.keep(upload={"filename": "contract.pdf", "media_type": "application/pdf",
                      "size": 84213, "sha256": "..."})
aizk.report(text="The settled claims in documents 0198... and 0199... contradict.")
aizk.share(documents=["019820a1-..."], scopes=["Book Club"])
```

`AizkMCP.user(context, identified=True)` is what enforces that column. It reads the caller bound by
the middleware and raises `ToolError` when an anonymous caller tries to write, so
`find` is the only tool the read-only anonymous identity can reach, and never for the web.

The resource template is `aizk://artifacts/{artifact_id}/contents/{artifact_content_id}`. It reads
one exact original revision, checks visibility with `user.exec[_ArtifactObject]` rather than in
Python, and attributes the read to the artifact's own scopes rather than the caller's.

## The upload mode

`keep` with an `upload` declaration is the one tool call that does not write memory. It refuses
to be combined with `source_uri`, `preserve_source`, `observed_at` or `expires_at`, mints a grant
through `UploadBox.mint`, and returns exactly this.

```python
UploadTicketAccepted(status="accepted", upload_url=..., expires_seconds=...)
```

The caller PUTs the bytes to that URL, which the HTTP API serves. Callers never build the URL
themselves.

## The middleware chain

```text
  tool call ──▶ IdentityMiddleware ──▶ CallerRateLimit ──▶ tool body ──▶ Memory ──▶ PostgreSQL RLS
```

:::note[The order is load bearing]
Identity resolves first so the rate limiter and the accounting both have a caller to key on. Run
them the other way and a rate check has nobody to charge.
:::

`IdentityMiddleware.resolve` calls `Auth.resolve()`, stores the `User`
on the request context under the `aizk_user` state key, opens an accounting context and a serving
span, measures the request JSON size, runs the handler, then queues one durable usage event with
the reply size. Because accounting happens after the handler returns, a failed call is not charged.

`CallerRateLimit` then reads that bound user and raises `ToolError` if none was resolved, which is
why it cannot run first. Its bucket is keyed by the aizk user ID, sized `round(rate * 5)` with a
refill of `mcp_request_rate_per_second`, and held in an `OrderedDict` capped at 4096 entries with
LRU eviction. This is burst control inside one process, not a durable quota. Tool calls and
resource reads drain the same bucket.

## Direct Logto authorization

`Auth.provider()` returns a FastMCP `RemoteAuthProvider` that names the tenant issuer as the
authorization server, or `None` when `logto_url` or `mcp_public_url` is unset, which is explicit
local mode. AIZK serves only MCP and RFC 9728 protected-resource metadata. Logto serves discovery,
authorization, tokens, and revocation.

Each supported MCP client uses a pre-registered public Native application and authorization code
flow with PKCE. The client ID is public configuration. There is no MCP client secret, dynamic
registration database, reference-token layer, or server-side OAuth session to persist. The client
requests `resource=settings.mcp_resource_id`, and AIZK accepts only a signed Logto token with that
audience and the `control` scope.

Caddy forwards only `/mcp`, `/mcp/*`, and
`/.well-known/oauth-protected-resource/mcp` to the MCP process.

## Errors are translated, never leaked

Every expected failure becomes a `ToolError` with text a model can act on, and the original is
chained as the cause. `keep` maps `MalwareRejectedError`, `MalwareUnavailableError`,
`ObjectStoreError`, `httpx.HTTPError` and `ValueError`, and the artifact resource maps
`IntegrityMismatch` and store outages to `ResourceError`. Nothing lets a stack trace or an internal
message reach a client.

## Sharing a service, not a layer

The tools do not implement memory. `AizkMCP.memory(user)` builds a `Memory` from
`src/aizk/memory.py` bound to that caller, and the HTTP API builds the same object from the same
class. Recall, ingestion, scope authorization and graph projection are defined once there, and each
transport keeps only its own identity resolution and its own input limits.

`src/aizk/mcp/ruff.toml` keeps that honest. It extends the root config and bans `sqlalchemy.select`,
`sqlmodel.select`, both `Session` types and `aizk.store.engine.Database` inside this package. A
transport that cannot build a statement or open a session has to go through a model classmethod or
`User.exec`, which is where the scope rules already live. `src/aizk/api/ruff.toml` carries the same
overlay.

## Next

<div class="not-content">

- [The HTTP API](/docs/dev/interfaces/http-api/) is the other transport over the same service.
- [The CLI](/docs/dev/interfaces/cli/) drives these tools from a terminal.
- [Layers and import contracts](/docs/dev/architecture/layers/) explains the ban lists in full.

</div>
