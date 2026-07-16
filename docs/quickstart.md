# Quickstart

You need one MCP URL. The lab deployment is available at
`https://aizk.phvv.me/mcp`. Account provisioning automatically grants each invited user the global
AIZK role and API permission needed to sign in from any supported client.

## Connect a client

Add the endpoint to Codex, Claude Code, or OpenCode with the project configuration on the
[MCP clients](mcp-clients.md) page. Then start OAuth from that client. No OAuth client ID or secret
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
effective `write:memory` permission can write to them. The current endpoint requires login and has
no anonymous or self-registration flow.

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
of an external website or paper PDF. The title belongs in the text, and authored notes do not need a
source URI.

Both `observed_at` and `expires_at` are optional. Omit them for ordinary durable knowledge. Set
`observed_at` only when a known applicability time differs materially from capture. Set
`expires_at` only when the outside world supplies a known time after which the information stops
being true. It is not a reminder or maintenance date. See [Observation and expiration](concepts.md#observation-and-expiration).

An optional `scopes` list chooses an authorized organization or intersection destination. Without
it, `remember` writes to the caller's private scope.

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

Copy `deploy/.env.example` to `.env`, fill the three independent database passwords, inspect the
model and storage settings, then start the single Compose file. Compose runs migrations, the
public request server, and the privileged worker as separate services from the same image.

```sh
docker compose --env-file .env -f deploy/docker-compose.yml up -d
```

Use `--profile public` only after the Logto, OAuth, public URL, and Cloudflare settings are
complete. The tunnel publishes Logto first, then the MCP origin stays offline until the
authentication preflight succeeds.

Check the whole deployment in under five seconds.

```sh
docker compose --env-file .env -f deploy/docker-compose.yml exec -T worker aizk db health
```

The report covers migrations, RLS, queue state, model aliases, context sizes, per-scope graph counts,
the last writes, and one bounded recall. Continue with [Operations](operations.md) for Logto, TLS,
backups, and production deployment.
