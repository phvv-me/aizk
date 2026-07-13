# Autonomy

Maintenance never runs in a caller request. A pgqueuer worker drains durable jobs and a cron
scheduler fans scheduled passes out once per exact scope set. Each job binds authority for only
that scope set before it opens an application session.

The scheduler discovers its roster from stored documents and unpromoted working memory under the
database administrator role. It does not need a user or organization table. Scope-keyed
watermarks debounce passes whose corpus has not changed.

Graph consolidation ranks only a bounded current fact set in PostgreSQL. The final transaction
locks each exact scope, subject, and perspective slot, reruns that ranking, and writes only when it
still matches the model-time snapshot. A concurrent change replans the candidate instead of
applying a stale ADD or UPDATE. Content rows mint in one batch on the normal path, while an actual
deterministic ID race falls back to isolated savepoints because RLS-hidden content cannot safely
use `ON CONFLICT`.

| Pass | Work | Trigger |
|---|---|---|
| graph build | gate, extract, consolidate, and write each pending chunk | ingest queue |
| session promotion | copy old or overflowing working memory into the graph pipeline | schedule |
| dedup | merge duplicate entity content without losing any scoped claim | schedule |
| decay | archive stale and rarely accessed facts from default recall | schedule |
| communities | detect entity clusters and write thematic summaries | growth gate |
| RAPTOR | build recursive summaries above communities | growth gate |
| profiles | refresh rolled-up entity descriptions | schedule and write queue |
| insights | derive higher-level observations and write them back as facts | schedule |
| self improve | score retrieval toggles and persist a significant winner | schedule |
| backup | write and prune database backups | schedule |

An A-and-B job binds A and B as its read authority, so RLS supplies A, B, and bridge knowledge. It
writes derived artifacts to the exact A-and-B scope set. Creator identity is provenance only and
is not the maintenance partition.

## Operations

`aizk db setup` migrates to head, installs the queue schema, and grants the application role.
`aizk db health` reports migration currency, row level security drift, row counts, queue depth,
and serving endpoint reachability.

The MCP server runs setup on startup unless `AIZK_AUTO_SETUP` is false. Operational commands are
CLI-only and are not registered on the network MCP server.
