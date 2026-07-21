---
title: "The HTTP API"
description: "The browser API, its routes, and why it is not a second engine."
---

This page assumes you have read [The MCP server](/docs/dev/interfaces/mcp/), because the two
transports share their authentication and their service layer, and this page only describes what
differs. The code is `src/aizk/api/` and it runs as its own process through
`aizk admin server api`.

## Who it is for

The API exists for the SvelteKit app and for nothing else. Agents talk MCP. That is why it is a
separate process from the MCP server in `src/deploy/docker-compose.yml`, and why the two can scale
independently while sharing one PostgreSQL upload capability table.

`AizkAPI.app()` assembles a plain FastAPI with explicit `add_api_route` calls rather than
decorators, so the whole surface is one readable list.

## The routes

| Method | Path | Handler | Returns |
|---|---|---|---|
| GET | `/healthz` | `health` | 204, no auth |
| GET | `/api/me` | `me` | `Me`, label and organization standing |
| GET | `/api/status` | `status` | `StatusReport`, `days` 1 to 365 |
| GET | `/api/overview` | `overview` | `Overview`, totals, usage, recent sources, artifacts |
| GET | `/api/usage` | `usage` | `UsageReport`, `days` 1 to 365 |
| GET | `/api/processing` | `processing` | `ProcessingReport` |
| GET | `/api/processing/events` | `processing_events` | `text/event-stream` |
| GET | `/api/sources` | `sources` | `SourcePage`, `search`, `limit` 1 to 100, `offset` |
| GET | `/api/findings` | `findings` | `FindingPage`, same paging |
| GET | `/api/subjects` | `subjects` | `SubjectPage`, same paging |
| GET | `/api/themes` | `themes` | `ThemePage`, every visible theme |
| GET | `/api/graph` | `graph` | `GraphSlice`, `limit` 1 to 80 |
| POST | `/api/recall` | `recall` | `Answer`, one Markdown string |
| PUT | `/api/uploads/{capability}` | `receive_upload` | `ArtifactReceipt` |
| GET | `/api/organizations` | `organizations` | `OrganizationDirectory` |
| POST | `/api/organizations` | `create_organization` | `OrganizationChange` |
| POST | `/api/organizations/{name}/members` | `add_member` | `OrganizationChange` |
| PUT | `/api/organizations/{name}/members/{member_id}` | `set_member_role` | `OrganizationChange` |
| DELETE | `/api/organizations/{name}/members/{member_id}` | `remove_member` | `OrganizationChange` |

Every route except `/healthz` and the upload PUT takes the `Verified` dependency, which reads an
`HTTPBearer` credential and resolves it through `Auth.bearer`, the same verifier the MCP server
uses. A missing or invalid token is a 401 before the handler runs.

## Not a second engine

The handlers are thin on purpose. Recall builds a `Memory` and calls it. The catalog routes call
`SourcePage.load`, `FindingPage.load`, `SubjectPage.load`, `ThemePage.load` and `GraphSlice.load`,
each of which runs one `user.exec[...]` over a statement defined on the `Explorer` namespace in
`src/aizk/store/models/namespaces.py`. Organization mutations go straight to `OrganizationManager`,
which talks to Logto.

`src/aizk/api/ruff.toml` enforces this. It bans `sqlmodel.select`, `sqlalchemy.select`, both
`Session` types and `aizk.store.engine.Database` inside the package, so a handler physically
cannot build a query or open a session.

Failures are translated once, by `status_for` and `detail_for`, which are registered as exception
handlers for eight types.

| Failure | Status |
|---|---|
| `UploadCapabilityError` | 410 |
| `ByteLimitExceeded` | 413 |
| `ValidationError`, `MalwareRejectedError` | 422 |
| `ScopeNotFoundError`, `PermissionError` | 403 |
| `MalwareUnavailableError`, `ObjectStoreError` | 503 |
| `httpx.HTTPError` | 502 |
| `ValueError` | 400 |

Domain messages pass through, and external failures are replaced with fixed text so an upstream
error never reaches a browser verbatim.

## The usage middleware

`UsageMiddleware` in `src/aizk/api/middleware.py` is a raw ASGI middleware, not a FastAPI one.

```text
  receive ──▶ measured_receive ──┐
                                 ├──▶ app ──▶ measured_send ──▶ buffer
  send    ◀── replay buffer ◀────┘                 │
                                                   ▼
                                       account(received, sent, elapsed, status)
```

It buffers every response message until the usage event is queued, then replays the buffer to the
real `send`. That ordering means a reply is never delivered before its accounting is durable.
`/api/processing/events` is the one exception, listed in `_STREAM_PATHS`, because buffering a
server-sent event stream would defeat it. That path gets the accounting context but no buffering.

## No docs URL, and a spec anyway

The app is built with `openapi_url=None`, `docs_url=None` and `redoc_url=None`. A browser API on a
public origin has no reason to publish an interactive schema, and Swagger UI is one more surface
to keep safe.

The spec is still generated, just not served. `aizk admin api openapi` builds one throwaway
`AizkAPI` around an `InertIntake`, calls `service.app().openapi()` and writes
`src/web/openapi.json`. `pnpm generate` in `src/web` then runs `@hey-api/openapi-ts` over that file
into `src/lib/api/generated`. The FastAPI response models are therefore the frontend's contract,
and a route whose return type changes breaks the TypeScript build rather than a page at runtime.

The `json_body` helper exists for the same reason. Handlers read and validate their own bodies
through `AizkAPI.payload`, which keeps the byte budget of `8 * mcp_remember_max_chars`, so the
request schema is declared explicitly with `openapi_extra` and its local `$defs` are inlined
before embedding.

## One route Caddy forwards

`src/deploy/Caddyfile` sends `PUT /api/uploads/*` to this process and answers **404** for every
other `/api` path at the edge. The API is not publicly reachable in general. The SvelteKit server
reaches it over the internal network from `AIZK_WEB_API_URL`, and the only thing that has to cross
the public boundary is an upload ticket redeemed by a CLI or an MCP client, which carries its own
one-time capability rather than a bearer token.

## Next

<div class="not-content">

- [The web app](/docs/dev/interfaces/web/) is the only consumer of these routes.
- [Artifacts](/docs/dev/write/artifacts/) covers what the upload PUT does with the bytes.
- [Deployment topology](/docs/dev/run/topology/) shows the processes and how Caddy routes them.

</div>
