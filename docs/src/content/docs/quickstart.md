---
title: "Quickstart"
description: "Connect an MCP client to the aizk deployment in about five minutes."
---

You need one MCP URL. The lab deployment is available at
`https://aizk.phvv.me/mcp`. Account provisioning automatically grants each user the global
`aizk-user` role and AIZK API `control` permission needed to sign in from any supported client.

## Connect a client

Add the endpoint to Codex, Claude Code, or OpenCode with the project configuration on the
[MCP clients](/mcp-clients) page. Then start OAuth from that client. No OAuth client ID or secret
belongs in the repository.

For Codex the login command is

```sh
codex mcp login aizk
```

Confirm the connection with

```sh
codex mcp list
```

Call `status` before the first shared write. It returns the caller's Logto profile and global roles.
Each organization includes its description, custom data, members, organization roles, effective
permissions, and a `writable` field. Private memory remains available by omitting `scopes`.
Public organizations are readable by every authenticated AIZK user, but only members with the
effective `write:memory` permission can write to them. The endpoint requires login and has no
anonymous public memory API. The optional browser exposes email-first Logto signup when the
deployment enables self-registration.

## Recall existing memory

Ask the client to call `recall` with a natural question. Aizk returns one prompt-ready string rather
than a transport-specific result tree.

```json
{
  "query": "What are my current active projects and their next actions?"
}
```

The response is one Markdown string. It lists only shared scopes involved in the result with their
descriptions, then gives numbered merit-ordered evidence with exact scope provenance. Omit the
optional `budget` unless repeated responses are too long.

## Remember durable context

Send self-describing Markdown. The first level-one heading becomes the retrieval title. A generic
source tag associates the note with any entity kind in the live ontology. A same-name tag declares
the heading itself as that kind. Typed relation lines remain available when an exact predicate is
important.

```json
{
  "text": "# Assay validation\n\n#project: Assay validation\n#area: Research\n\n- part_of [Area] Research\n- has_status [Status] Active\n\nThe team selected the current assay plan."
}
```

AIZK validates tag kinds, declared types, and predicates against its database-backed ontology. The
general tag form is `#<kind>: <entity name>`. Project and Area are examples rather than special
document fields. A supporting note can use `#project: Assay validation` while keeping its own title.
Tags express association and never imply status or write scope. Other valid kinds and relations use
the same syntax, and ordinary notes need no declaration. Use `source_uri` only for the original URL
of an actual external source. The title belongs in the text, and authored notes do not need a
source URI.

Both `observed_at` and `expires_at` are optional. Omit them for ordinary durable knowledge. Set
`observed_at` only when a known applicability time differs materially from capture. Set
`expires_at` only when the outside world supplies a known time after which the information stops
being true. It is not a reminder or maintenance date. See [Observation and expiration](/concepts#observation-and-expiration).

An optional `scopes` list chooses an authorized organization or intersection destination. Without
it, `remember` writes to the caller's private scope.

## Add a file or external source

Open the personal dashboard to upload one bounded file or submit one public HTTPS URI. Choose
private memory or one writable organization. The browser shows queued, processing, ready, and
failed states. It never exposes the worker's internal graph machinery.

Text remains the preferred input. Preserve a source when its exact original may be needed later,
such as a contract, form, paper, signed record, or presentation. An MCP client can submit a website
or file URL without manufacturing text.

```json
{
  "source_uri": "https://example.org/paper.pdf",
  "scopes": ["Research Lab"]
}
```

Omit `text` in this form. AIZK fetches the source once, validates the network destination, scans
the bytes, stores an immutable original, and returns a queued artifact receipt. PgQueuer sends the
original content ID to the worker. Docling converts the stored bytes to native JSON and normalized
Markdown, after which the ordinary text pipeline makes the content recallable.

Add `text` and set `preserve_source` to true when the text should be companion context for the same
file. Files are limited to 10 MiB. Compressible originals use Zstandard in object storage while
their original digest and size stay authoritative. Markdown, Docling JSON, companion text, and
metadata stay in PostgreSQL. If Docling cannot parse a file, filename, size, media type, URI, and
companion context still form a recallable fallback document.

Recall stays text first. It may include an `aizk://artifacts/.../contents/...` resource identifier
for evidence grounded in a file. Read that resource only when the task needs original bytes. The
resource always names the exact revision that grounded the evidence and applies current row
security before transfer.

## Share memory

`share` copies visible documents into one authorized scope set and preserves their provenance. It
never moves or broadens the source row.

```json
{
  "documents": ["019b2d0a-1d42-7d6e-a9aa-8f8443ec6f4a"],
  "scopes": ["Research Lab"]
}
```

## Run your own stack

Copy `src/deploy/.env.example` to `.env`, fill the three independent database passwords, independent
object-store credentials, and the Docling API key. Inspect the model, upload, ClamAV, and storage
settings, then start the single Compose file. Compose runs migrations, the public request server,
and the privileged worker as separate services from the same image. SeaweedFS, ClamAV, and Docling
remain private on the Compose network. PostgreSQL and PgQueuer own all durable workflow state, so
the stack has no Redis service. The browser interface keeps only transient presentation state
and reconstructs it after a restart.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml up -d
```

Use `--profile public` only after the Logto, OAuth, public URL, and Cloudflare settings are
complete. The public profile also starts the optional SvelteKit browser interface and its
browser JSON API. Create a Logto Traditional Web application for the interface and register one
exact redirect URI.

```text
https://aizk.phvv.me/auth/sign-in-callback
```

Set `AIZK_WEB_PUBLIC_URL` to the public HTTPS origin. Place the application ID and secret in
`AIZK_WEB_CLIENT_ID` and `AIZK_WEB_CLIENT_SECRET`. Generate an independent
`AIZK_WEB_SESSION_SECRET` with at least 32 bytes. It must differ from every Logto and OAuth client
secret.

Use `https://auth.phvv.me` for Logto. Set the browser and MCP public URLs to
`https://aizk.phvv.me`. Cloudflare Tunnel maps `auth.phvv.me` to `logto:3001` and
`aizk.phvv.me` to `caddy:8081`. Caddy forwards the MCP and OAuth routes to `server:8080`,
forwards `/api` to the API service, and every remaining path to the SvelteKit server. The
SvelteKit server keeps the Logto session in an encrypted HttpOnly cookie and sends the API a
short-lived bearer token, which resolves the current Logto account and organization authority on
every request. A missing or suspended account and a user without `aizk-user` are rejected.

The tunnel publishes Logto first, then the MCP and browser origins stay offline until their
authentication preflight succeeds. The hosted Logto sign-up page is email first. A new account
receives the default `aizk-user` role but no organization membership, so private memory works
immediately while shared writes still require an organization role with `write:memory`.
Organization administrators add only an existing Logto account by exact email. AIZK does not send
invitations, expose the tenant user directory, or allow removal or demotion of the final admin.

Check the whole deployment in under five seconds.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml exec -T worker aizk db health
```

The report covers migrations, RLS, queue state, model aliases, context sizes, per-scope graph counts,
usage by caller and target scope, logical and physical file bytes, the last writes, and one bounded
recall. Continue with [Operations](/operations) for Logto, TLS,
backups, and production deployment.
