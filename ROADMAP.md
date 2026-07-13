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

- [ ] Run bounded GroupMemBench smoke cells on crimson, then the complete four-domain matrix.
- [ ] Add a flat baseline over raw messages, summaries, facts, and keywords as independent keys.
- [ ] Ablate one-hop expansion, personalized PageRank, communities, RAPTOR, profiles, reranking,
  and context ordering independently.
- [ ] Add Memora criteria and LongMemEval-V2 state, workflow, gotcha, and premise adapters.
- [ ] Add Mem2ActBench once evaluation can judge tool selection and arguments.
- [ ] Record positive evidence and obsolete negative evidence per benchmark case.

## Product hardening

- [ ] Add authenticated invalidation for the fail-closed public organization directory.
- [ ] Add an import counterpart to scoped export.
- [ ] Finish narrow erasure and collect immutable content rows left without claims.
- [ ] Replace remaining migration-only PostgreSQL DDL strings with reusable SQLAlchemy DDL
  elements where the extension APIs permit it.
- [ ] Freeze the MCP and operator surfaces only after the benchmark results settle the defaults.
