# Autonomy

Maintenance never blocks a caller. A pgqueuer worker drains durable jobs, and a cron scheduler
fans scheduled passes out per user, so a pass can never leak across tenants. Each pass is
debounced on a watermark, so an unchanged corpus costs nothing.

| Pass | What it does | Trigger |
|---|---|---|
| graph build | gate, extract, cascade per pending chunk, resumable per chunk | queue, on ingest |
| session promotion | working-memory rows promoted into the graph by age or overflow | scheduled |
| dedup | merge duplicate entities by normalized name and type | scheduled, watermark |
| decay | archive stale, rarely-accessed facts out of default recall | scheduled |
| communities | detect entity clusters and write thematic summaries | growth-gated |
| RAPTOR | recursive summary tree above the communities | growth-gated |
| profiles | rolled-up per-entity portraits, static identity plus live state | scheduled |
| insights | higher-level observations derived from the graph, written back as facts | scheduled |
| curation review | the standing LLM reviewer judges pending claims against group canon | pending-count debounce |

## Operations

Operations collapsed to two admin verbs. `setup()` migrates to head, installs the queue
schema, and applies the app-role grants, idempotently, and runs automatically at server start
(skippable with `AIZK_AUTO_SETUP`). `health()` reports migration currency, the RLS drift
verdict, row counts, queue depth, and model-endpoint reachability in one structured JSON.

A fresh deployment is therefore `docker compose up` plus `serve-mcp` and nothing else. The
server self-checks, self-migrates, and an admin interrogates state over MCP without touching
a shell.
