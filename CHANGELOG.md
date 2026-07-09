# Changelog

All notable changes to aizk are documented here.

The format follows Keep a Changelog, and releases are cut from the version in `pyproject.toml`.

## Unreleased

### Security

- Closed an RLS write-policy bypass: an empty scope set made `scopes <@ writer_groups` trivially
  true, so any authenticated caller could write into another user's private space. The empty-scope
  write branch is now guarded on ownership.
- The MCP server validates a token's `aud` against its RFC 8707 resource id, so a token the issuer
  signed for another resource in the same tenant is rejected rather than accepted.
- A malformed identity-provider groups claim is skipped rather than crashing every authenticated
  request the token makes.

### Changed

- The session engine is reworked into composable building blocks and renamed for the row-level
  distinction it encodes: `acting_as`/`as_system` run as the RLS-enforced app role `aizk_app`,
  while `bypass_rls` runs as the owner role `aizk_admin` (formerly `aizk`) for the few cross-tenant
  content writes the app role's policies forbid.
- Membership roles adopt Logto's own `viewer`/`editor`/`admin` names, so a token's org role folds
  in with no translation; `editor` or `admin` may write, `viewer` only reads.
- Groups are Logto-only: every group is the local projection of a Logto organization
  (`oidc_org_id` required and unique), minted by `User.sync_groups`, which now writes only when the
  token's memberships actually changed rather than on every request. Hand-created groups and the
  `aizk group create` verb are gone.
- The `users` table is renamed `user_` (USER is a reserved word, matching `group_`).
- Ontology names are canonicalized to snake_case at write time, deduping the case and spacing
  variants a case-sensitive name key used to fork into separate rows.
- Store operations read the open session from a task-local `session()` accessor, ids use uuid7,
  and a deleted group's scoped rows fall back to private through a `BEFORE DELETE` trigger so the
  demotion fires on every delete path.

### Removed

- The curation-review loop in full: the `reviewed_at` gate, the `curated` group flag, the
  `pending`/`approve`/`reject` MCP verbs, the standing-reviewer background pass, and the
  server-wide `is_admin` flag that existed only for its cross-tenant reach. A write is canon the
  moment it lands.

### Fixed

- Retrieval: gap-fill truncates to the requested `k`, rerank guards a score-count mismatch, and a
  pagerank non-convergence degrades instead of failing the whole recall.
- Extraction: consolidation checks every same-predicate claim, a non-UTF-8 file no longer aborts a
  directory ingest, and the community/RAPTOR growth watermark stays monotonic under decay.
- The GLiNER2 relevance gate is re-enabled on the classification head with a `Person` floor and
  loads offline from a persistent cache; structural kinds no longer leak into the auto-create pool.
- `admin.publish_group` called a since-removed `Group.publish`; it now flips visibility through
  `toggle_public`.

### Migrations

- `0003` re-applies the corrected scoped RLS. `0004` canonicalizes ontology names to snake_case.
  `0005` drops the curation and server-admin columns and policies, renames the role enum values
  and the user table, recreates the recall view and function without the review gate, and installs
  the group-delete trigger. `0006` makes `oidc_org_id` required.

## 0.0.1 - 2026-07-04

### Added

- The content and claim store, entities and facts split into immutable content rows plus
  per-container bi-temporal claims, so identical knowledge extracted twice never duplicates.
- The scope-set lattice, `scopes uuid[]` on every row, forced Postgres row level security
  compiled from the models, and implicit intersection scopes for groups with no standing group
  of their own.
- The write path, chunking, a GLiNER2 gate, one combined extraction call, and a rules-first
  consolidation cascade, averaging 1.22 LLM calls per chunk.
- The read path, `recall()` fusing dense, lexical, graph-neighbor, community, RAPTOR, and
  profile lanes behind one hybrid Postgres function plus a cross-encoder rerank.
- Autonomy, a pgqueuer-backed worker and cron scheduler driving graph build, session
  promotion, dedup, decay, communities, RAPTOR, profiles, insights, and curation review.
- 36 MCP tools over FastMCP, everyday memory verbs, group governance, and root-only
  maintenance and admin, with Zitadel or local API-key identity.
- The eval harness, hit@k/nDCG@k/MRR scoring, a config sweep, and EverMemBench/TEMPO dataset
  loaders gated behind `AIZK_BENCHMARKS_ENABLED`.
- Documentation at [phvv.me/aizk](https://phvv.me/aizk), the engine explained in five parts,
  a paper-by-paper provenance map, and measured benchmarks and comparisons.
