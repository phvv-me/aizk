# Roadmap

This roadmap separates shipped behavior from hypotheses that still need measured evidence.

## Shipped

- [x] Logto tokens are the only source of user, organization, role, and public organization state.
- [x] Nonempty scope sets represent personal memory, organization memory, and organization
  intersections under forced PostgreSQL RLS.
- [x] Speaker snapshots and epistemic kinds survive capture, extraction, consolidation, recall,
  and context rendering without creating identity tables.
- [x] Objective facts consolidate at world scope while experiences, observations, opinions, and
  preferences remain distinct per speaker.
- [x] Backdated updates become history instead of replacing newer live state.
- [x] The hybrid retrieval plan is one typed SQLAlchemy statement with no handwritten runtime data
  query.
- [x] GroupMemBench imports real message histories into isolated shared scopes and evaluates each
  question as its named asking user.
- [x] FAMA scoring penalizes obsolete memory through explicit absence criteria.
- [x] Graph writing, graph repair, retrieval reads, and retrieval orchestration have separate
  modules.
- [x] Alembic autogenerate reports zero model drift on a fresh database.
- [x] Resolve current organization memberships and roles from Logto by verified subject with a
  short fail-closed authority cache.
- [x] Read the complete User authority through RLS, default writes to personal memory, and share
  into explicit organization destinations through provenance-linked copies.

## Measure next

- [ ] Run bounded GroupMemBench smoke cells on an available GPU host, then the complete
  four-domain matrix.
- [ ] Add a flat baseline over raw messages, summaries, facts, and keywords as independent keys.
- [ ] Ablate one-hop expansion, personalized PageRank, communities, RAPTOR, profiles, reranking,
  and context ordering independently.
- [ ] Add Memora criteria and LongMemEval-V2 state, workflow, gotcha, and premise adapters.
- [ ] Add Mem2ActBench once evaluation can judge tool selection and arguments.
- [ ] Record positive evidence and obsolete negative evidence per benchmark case.

## Investigate before building

- [ ] Evaluate managed workstreams for switching between native Codex and Claude sessions without
  losing operational context, using [ai-memory](https://github.com/akitaonrails/ai-memory) as prior
  art rather than as a dependency.
  - Keep an ordered workstream ledger separate from `SessionItem`, durable documents, and graph
    enrichment.
  - Prototype private-only, read-only client adapters with deterministic event IDs, import and
    delivery cursors, bounded handoffs, one active-writer lease, and repository checkpoints.
  - Measure transcript volume, storage growth, prompt cost, recall quality, graph queue impact,
    client format stability, crash recovery, and concurrent handoff behavior.
  - Threat-model secrets, prompt injection, completed tool-call replay, organization sharing, and
    authorization boundaries before enabling capture outside private memory.
  - Require an explicit architecture decision after the prototype. Reject the feature if it cannot
    preserve native client ownership, avoid automatic raw-event promotion, and fail closed on
    uncertain or private transcript records.

## Product hardening

- [ ] Add authenticated invalidation for the fail-closed public organization directory.
- [ ] Add an import counterpart to scoped export.
- [ ] Finish narrow erasure and collect immutable content rows left without claims.
- [ ] Replace remaining migration-only PostgreSQL DDL strings with reusable SQLAlchemy DDL
  elements where the extension APIs permit it.
- [ ] Freeze the MCP and operator surfaces only after the benchmark results settle the defaults.
- [ ] Revisit the identity namespace before any second deployment shares data with this one.
  `_IDENTITY_NAMESPACE` is frozen at a URL shaped name and every user id and scope id is a
  `uuid5` of it, so it is stored data rather than configuration and cannot move without minting
  a disjoint set that matches nothing already written. That is settled for this deployment and
  costs nothing, including if the domain in it is retired, because the string is a name and never
  resolves. It becomes a real question only in two cases, merging two deployments that were
  seeded differently, or offering a hosted multi-deployment product where one namespace per
  tenant would be wanted from the first write. Neither is close, and the migration runbook covers
  the domain move that is.
