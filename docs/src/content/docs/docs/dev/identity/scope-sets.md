---
title: "Scope sets in depth"
description: "The lattice, the array invariant, and the five rules the policies encode."
---

[Scopes](/docs/user/concepts/scopes/) explains the idea, that a memory carries a set of
organizations and you read it only if you belong to every one of them. This page is the same idea
at the level of the column, the policy expression and the classmethods, and it assumes you have
read that page. Where the values come from is on [The Logto boundary](/docs/dev/identity/logto/).

## The column

Every scoped table gets two columns from the `Scoped` mixin in `src/aizk/store/mixins/scoped.py`.

```python
created_by = sql.Field(UUID5, index=True)
scopes = sql.Field(list[UUID5], min_length=1, sa_type=ARRAY(Uuid()))
```

The array is nonempty and stored sorted. Nonempty is enforced twice, by `min_length=1` in the
model and by `cardinality(scopes) > 0` inside the policy itself, because a zero-length array would
be trivially contained in any set and would read as visible to everyone.

Sorted is a convention that every writer follows, and you will see `sorted(scopes)` or
`sorted(scopes, key=str)` at each write site in `src/aizk/artifacts/`, `src/aizk/graph/` and
`src/aizk/extract/ingest.py`. It exists so that two different callers who name the same two
organizations produce the same array, which makes exact-set equality a plain `=` comparison. In
Python the same value is a `frozenset[UUID5]`, aliased as `Scopes` in `src/aizk/types.py`.

## Why it stays an array

A join table is the obvious alternative and it is the wrong one here, for two reasons.

The first is the policy. The read check is one array containment test, `scopes <@ readable`, which
PostgreSQL evaluates per row against a value already sitting in a transaction-local setting. With
a join table every policy on every table would become a correlated subquery with a grouped
`HAVING`, evaluated inside row security, on every read.

The second is that a scope set is a value, not a relationship. Background work partitions by exact
scope set and asks questions like `Fact.Claim.scopes == sorted(scopes)`. That is an equality on
one indexed column. Expressed relationally it is set equality, which is an aggregate comparison
and cannot be an index lookup.

## The predicate

`Scoped.__rls__` compiles the policies. The caller's standing arrives as the `app.scopes` setting,
which `rls.Context` writes with `SET LOCAL`, and `_authority` turns each JSON key of that setting
back into a native `uuid[]` so the containment operators work on the right type.

```text
  read a row?
    │
    ├── cardinality(scopes) = 0 ?              ──▶ no
    │
    ├── scopes <@ app.scopes->'read' ?         ──▶ yes
    │
    ├── cardinality(scopes) = 1
    │   AND scopes <@ app.scopes->'public' ?   ──▶ yes
    │
    └── otherwise                              ──▶ no

  write a row?  nonempty AND scopes <@ app.scopes->'write'
```

That cardinality guard on the public branch is the whole reason public organizations stay
harmless. A public organization opens exactly the rows scoped to it alone. A row scoped to
`{public_org, private_org}` is an intersection, and an intersection is narrower than either side,
so it must not fall out of the public branch. Requiring cardinality 1 there is what keeps it from
doing so. The tests that cover this still carry the older name for the shape, in
`tests/store/test_rls.py::test_scope_intersections_and_public_access_follow_the_lattice`.

`Scoped` emits `scope_read` and `scope_insert` always, adds `scope_update` when the model declares
`mutable` and `scope_delete` when it declares `deletable`, all bound to `settings.app_role`.
Setting `read_through` replaces the child's read predicate with "my parent is visible", and adds to
its write predicate that `(parent_id, scopes)` must match a visible parent row exactly. That pairing
is what stops a chunk from being written with a wider array than the document it belongs to.

## The caller side

`User.scopes` is a `ScopeTable`, three frozensets named read, write and public. Three
classmethods build one.

| Constructor | Read | Write |
|---|---|---|
| `User.authorized(id, read=, write=, public=)` | as given | as given |
| `User.private(id)` | just `{id}` | just `{id}` |
| `User.system(scopes)` | exactly `scopes` | exactly `scopes` |

`authorized` is called only after an authentication boundary has already verified every set, and
it is where `OrganizationStanding.writable` gets stamped from the write set. `private` is the
auth-off local identity. `system` is background authority and is covered on
[Background work](/docs/dev/identity/background/).

Three ways to run a statement as that caller, all of which write the same setting.

`async with user as session` opens one short app-role transaction. Nested uses are tracked on a
`ContextVar` stack, so concurrent tasks cannot pop one another's transaction.

`await user.exec[Model](statement, **binds)` runs exactly one statement in its own transaction and
validates every row into `Model`. Binds the statement names are filled from settings first and
then overridden by the keywords, so a changed setting takes effect on the next call.

`user.owner` opens an RLS-bypassing transaction on the owner engine and raises `PermissionError`
unless the caller is `settings.system_user_id`. It is for migrations, the roster read and
maintenance, never for a request.

## The five rules

**Read.** You may read a row only when every scope in its array is in your read set. Nothing else
grants a read.

**Write.** You may insert, and update or delete a mutable or deletable row, only when every scope
in its array is in your write set. Before SQL is reached, `User.write_scope(names)` already refuses
an organization name you do not hold or cannot write to, raising `ScopeNotFoundError`.

**Retrieval.** Recall takes no scope argument. It runs under the caller's whole read set, so one
question spans private memory, every organization, and every intersection at once, and the
returned evidence is labeled by `User.scope_labels` as Private, the organization name, or Shared.

**Provenance.** `created_by` says who and `scopes` says where, and they are independent. Content
rows carry no scopes at all and inherit visibility from the claims that point at them, which
[The data model](/docs/dev/store/data-model/) explains.

**Public.** A public organization grants read to any signed-in caller, only for rows whose array
is exactly that one scope, and never grants write. The anonymous fallback identity gets a public
read set and empty read and write sets.

## Next

<div class="not-content">

- [Row level security](/docs/dev/store/rls/) covers how policies are generated and verified.
- [The data model](/docs/dev/store/data-model/) covers why content rows have no scopes.
- [Background work](/docs/dev/identity/background/) covers scope sets without a caller.

</div>
