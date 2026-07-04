# API

The public surface is the MCP tool set. Any [MCP](concepts.md#agent-and-mcp)-capable assistant,
or a bare `fastmcp.Client` as shown on the [home page](index.md#use), calls these directly with
no Python import beyond that one client.

## Everyday verbs

| tool | does |
|---|---|
| `recall(query, scopes, k)` | the one retrieval verb, five fused lanes, warm around 0.3 to 0.5 s |
| `remember(text, scopes, kind)` | write text as fast working memory |
| `reference(uri, scopes)` | record a paper, url, or file as recallable |
| `ingest(path, scopes)` | ingest a file or directory into the graph pipeline |
| `ingest_image(path, scopes)` | ingest an image for the vision-embedding lane |
| `get_context(query, scopes)` | assemble one token-budgeted, prompt-ready context pack |
| `timeline(since_days, entity, scopes)` | the weekly-review view, newest facts first |
| `projects(scopes)` | every visible Project entity with its profile and recent facts |

## Groups and governance

| tool | does |
|---|---|
| `create_group(name, public, curated)` | create a sharing group |
| `publish_group(group, public)` | make a group's singleton scope public |
| `curate_group(group, curated)` | require review before a claim in this group is visible |
| `add_member` / `remove_member` | manage reader, writer, or admin membership |
| `pending(group)` | a curated group's unreviewed claims |
| `approve` / `reject` | resolve a curated group's pending claims |
| `promote(document, to_scopes)` | publish an audited copy into a wider scope set |
| `delete_group(group)` | demote every scope set containing this group to private, never widen |

See [Lattice](engine/lattice.md) for why membership, not a standalone group, decides visibility.

## Maintenance and admin

Registered only for the resolved root principal.

| tool | does |
|---|---|
| `setup()` / `health()` | migrate, install the queue, grant roles / report readiness in one call |
| `tasks_status()` | background-pass watermarks and queue depth |
| `audit(limit)` | recent writes, owner, scope, and promotion provenance |
| `force_rebuild` / `force_decay` / `force_raptor` / `force_reembed` | run a background pass now rather than wait on its schedule |
| `bench` / `sweep` / `benchmark` / `scale` | the eval harness, gated by `AIZK_BENCHMARKS_ENABLED` where noted |
| `export_scope(path)` | dump one scope's claims and history to a file |
| `profile_report()` | span-profiler timing stats, when `AIZK_PROFILING=1` |
| `create_user` / `grant_admin` / `list_groups` / `list_principals` | identity and group bootstrap |

`scopes` parameters take a comma-separated list of group names. `scopes="finance,business"`
writes or reads the intersection only members of both groups can see.

## The CLI

The CLI is a process and bootstrap entrypoint only, no engine-verb mirror, everyday memory
calls always go through the MCP tools above.

| command | does |
|---|---|
| `aizk serve-mcp` | run the MCP server, stdio or HTTP with `AIZK_MCP_HTTP=1` |
| `aizk worker` | drain the durable background-pass queue |
| `aizk migrate` / `aizk makemigrations` | apply or author a schema migration |
| `aizk check-rls` | diff compiled row level security policies against the live catalog |
| `aizk install-queue` | install the pgqueuer schema once |
| `aizk create-user <name>` | bootstrap the first principal before any auth exists |
| `aizk scale` | run the scale eval harness |
| `aizk recall-context` / `aizk capture-session` | the Claude Code session hook bridges |
