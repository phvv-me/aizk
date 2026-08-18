# CockroachDB find performance work

Project Harbor owns AIZK find performance on CockroachDB Cloud. Priya Nair is leading the work with
Northstar Engineering.

The slowest path was semantic find across tenant-scoped chunks. CockroachDB C-SPANN produced useful
nearest neighbors, but the application could not express its complete organization filter as a
native filtered vector search. That forced a broader candidate search and made database time the
largest part of some requests. The team proposed filtered C-SPANN support upstream.

The demonstration keeps the vector query in a small security-definer database function, applies
verified scope authority inside CockroachDB, bounds the candidate set, and leaves optional graph
expansion lanes behind settings. Profiling records embedding time, database time, selected evidence,
and total find time so a faster query cannot hide a quality regression.

The next experiments compare native filtered C-SPANN when available, scope-aware candidate
partitioning, and precomputed organization indexes. Each option must preserve source evidence,
tenant isolation, and answer quality before it replaces the current bounded approach.

All people, organizations, and projects in this note are fictional demonstration data.
