# The store

## Content and claim, a union model

Knowledge splits into two tables per kind. Content is the immutable structure, an entity's
normalized name and type or a fact's resolved subject ID, predicate, resolved object ID, and
statement. A `uuid5` addresses those canonical fields. Using resolved endpoint IDs prevents two
same-named entities with different ontology types from collapsing into one fact. Two people
independently extracting the same knowledge mint the same row with no lookup and no coordination.
Claims are per-container stakes on that content, carrying creator provenance, the scope set, the
bi-temporal ranges, and access counters. Dedup across the whole tenancy happens by construction,
and nobody's claim leaks through anyone else's.

RLS hides shared content until the caller has a readable claim. PostgreSQL therefore cannot use
`ON CONFLICT` for content IDs because conflict arbitration applies the table's `SELECT` policy.
`ClaimedContent.mint()` follows SQLAlchemy's PostgreSQL SAVEPOINT pattern instead. It rolls back
only a duplicate key, preserves the surrounding claim transaction, and propagates every other
integrity failure.

```mermaid
flowchart LR
    A[claim<br/>scopes finance] --> C{{content<br/>uuid5 of text}}
    B[claim<br/>personal subject scope] --> C
    C --> E[embedding<br/>stored once]
```

## Bi-temporal claims

Every fact claim carries two independent `tstzrange` dimensions. `valid` says when the fact
holds in the world and `recorded` says when this version sat in memory. Nothing is deleted.
A superseding write closes the `recorded` upper bound and inserts a fresh version, so history
is just the closed rows, point-in-time replay is a range predicate the GiST index answers
(measured at 30 ms over 300k claim versions), and Allen-algebra queries come free with the
type.

```mermaid
gantt
    dateFormat YYYY-MM-DD
    axisFormat %b %d
    section valid
    fact holds in the world       :2026-05-01, 45d
    section recorded
    version 1, superseded          :done, 2026-05-03, 20d
    version 2, live                :active, 2026-05-23, 30d
```

## The identity rule

Value objects get `uuid5` and events get `uuid7`. Content is what it says, so its id derives
from the text and a rerun converges on the same graph. A claim is the event of someone saying
it, so its id carries a timestamp prefix and lands writes on one edge of the index. The one
fixed id is a deterministic UUID5 anonymous sentinel. Logto subjects and organizations use UUID5 under
the standard URL namespace with separate user and organization paths.

## Declarative everything

SQLModel classes are the single source of truth. Foreign keys and indexes are field kwargs,
one `Timestamped` mixin carries both audit stamps, and views are first-class citizens. A
`ViewBase` subclass declares typed fields plus the `Select` that is the view and registers itself
when its class body ends. Typed SQLAlchemy DDL compiles it with `security_invoker` so a
view can never accidentally bypass row security. The whole schema regenerates from the models,
and the drift probe diffs compiled RLS policies against the live catalog through sqlglot and
must come back empty.

## The SQLModel boundary

SQLModel owns table entities, relationships, field constraints, ordinary selects, and the async
session API. Normal model reads should use SQLModel `select` with `AsyncSession.exec`, which keeps
scalar model results direct and typed. SQLAlchemy Core remains the narrow escape hatch for
PostgreSQL array and range operators, CTEs, conflict-aware bulk DML, views, migration DDL, and RLS
expressions.

Patos `FrozenModel` owns immutable values that cross service boundaries, including verified token
standing, queue payloads, and retrieval results. SQLModel table entities remain mutable because
the ORM unit of work manages their database state. Keeping these two roles separate avoids a
second request-model hierarchy without asking one base class to serve incompatible purposes.

Reusable PostgreSQL typing and expression helpers live in the optional `patos[sql]` extra. AIZK
imports the single `patos.sql` namespace for typed columns, JSONB reads, cosine distance, native
enums, t-string expressions, typed `VALUES` relations, and database hashing. Domain models expose
cohesive namespaces such as `Entity.Kind`, `Entity.Content`, `Fact.Claim`, `Fact.Live`, and
`Relation.Policy` instead of a flat list of closely related classes.

Document content identity is a native PostgreSQL UUID. `sql.uuid8` hashes the source with SHA-256
inside PostgreSQL, takes its first 128 bits, and sets the RFC 9562 version and variant fields. The
stored UUID carries 122 digest bits, uses PostgreSQL's compact UUID comparison and indexing, and is
validated as Pydantic `UUID8` whenever it crosses the model boundary.
