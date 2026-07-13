# aizk-ts

The aizk server in TypeScript, sharing the SvelteKit build with `personal/my` so the user
dashboard and the memory API grow from one codebase. The Python server at `../aizk` is the
reference implementation, and every ported surface is verified row for row against it.

- `src/lib/server/db/schema.ts` owns the schema and row-level security in drizzle, and
  `drizzle/` holds the generated plus runtime-surface migrations.
- `src/lib/server/recall/` is the recall program and its orchestration, including the
  cross-encoder rerank path and the packer twin.
- `src/lib/server/serving.ts` holds the HTTP clients for the embedder, the gliner sidecar,
  the reranker, and the structured LLM call.
- `src/mcp/` serves the MCP verbs over streamable HTTP.
- `scripts/` are the parity and smoke harnesses (`chefe run parity`, `share-check`).

Tasks live in `chefe.toml` (`dev`, `build`, `check`, `lint`, `parity`, `mcp`). The recall
query stays a `sql``` template by design, and the treesitter `sql` parser highlights it in
the editor (`:TSInstall sql` once, already installed here). Drizzle's query builder is used
where it fits; `insert().select()` cannot express partial-column copies (it demands every
table column, including generated ones), so the provenance-copy statements in `promote.ts`
stay raw SQL.
