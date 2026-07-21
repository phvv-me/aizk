---
title: "Layers and import contracts"
description: "The enforced layer stack and the two import contracts that keep it honest."
---

aizk has a layered architecture, and unlike most layered architectures this one is executable.
Two [import-linter](https://import-linter.readthedocs.io/) contracts live in `[tool.importlinter]`
in `pyproject.toml`, and they fail the lint gate the moment a package imports upward or reaches
past its allowed door. So the diagram below is not a wish, it is what the build enforces. This
page assumes you have read the [System map](/docs/dev/architecture/system-map/) and know roughly
what each package does.

Run the gate from the monorepo root.

```
chefe run lint-imports-aizk
```

CI runs the same check as `lint-imports` inside the lint job.

## The stack

Dependencies point strictly downward. A package may import anything beneath it and nothing above
it. Inside a layer, `|` siblings may not import each other at all, while `:` siblings may.

```d2
direction: down

l1: "cli : commands" { a: cli; b: commands }
l2: "admin | client | runtime" { a: admin; b: client; c: runtime }
l3: "mcp | api | ops" { a: mcp; b: api; c: ops }
l4: "memory" { a: memory }
l5: "status" { a: status }
l6: "artifacts : background : graph : retrieval : extract : serving : ontology : usage" {
  a: artifacts
  b: background
  c: graph
  d: retrieval
  e: extract
  f: serving
  g: ontology
  h: usage
}
l7: "auth | backup | export" { a: auth; b: backup; c: export }
l8: "integrations | storage" { a: integrations; b: storage }
l9: "store" { a: store }
l10: "config | types | exceptions | provenance | common" {
  a: config
  b: types
  c: exceptions
  d: provenance
  e: common
}

l1 -> l2 -> l3 -> l4 -> l5 -> l6 -> l7 -> l8 -> l9 -> l10
```

Three things in that picture are worth saying out loud.

`mcp` and `api` are independent of each other, so neither transport can grow a dependency on the
other and they meet only at `memory`, the one service both call. `runtime` sits above the
transports because it assembles them. And `config` is a leaf that imports nothing internal, which
follows from its independent bottom-layer placement rather than from a separate rule, so any
module anywhere can read settings without dragging the engine along.

## Exhaustive means a new package fails the gate

The layers contract sets `exhaustive = true`, which tells import-linter that the `layers` list
must name every top-level module inside the `aizk` container. Add `src/aizk/newthing/` and the
contract does not quietly ignore it, it fails, and the only way to make it pass is to decide
which layer the new package belongs to and write it down.

:::caution[The stack cannot drift by accretion]
A new top-level package is a build failure until you place it in the stack. That is deliberate.
Accretion is the usual way a clean layering rots, so here accretion simply does not compile.
:::

Two other settings shape what counts as an import.

`include_external_packages = true` pulls third-party packages into the graph, which is what makes
the second contract below able to see `sqlmodel` and `sqlalchemy` at all.

`exclude_type_checking_imports = true` means an import guarded by `if TYPE_CHECKING` is invisible
to the contract. This is not a loophole so much as a recognition that a type-only import creates
no runtime dependency, and the codebase uses it deliberately. `src/aizk/background/schedule.py`
imports `Runtime` under `TYPE_CHECKING` even though `runtime` sits several layers above
`background`, because the worker is handed a runtime it never constructs.

## The second contract, SQL stays where it belongs

The other contract is a `forbidden` contract. Seventeen named packages may not import `sqlmodel`
or `sqlalchemy` at all.

```
aizk.api        aizk.auth       aizk.client      aizk.cli
aizk.common     aizk.commands   aizk.exceptions  aizk.integrations
aizk.mcp        aizk.memory     aizk.provenance  aizk.runtime
aizk.serving    aizk.storage    aizk.status      aizk.types
aizk.usage
```

Those packages reach the database only through model classmethods and `User.exec`. The twelve
packages missing from that list are the documented exceptions, and they are exceptions for a
reason rather than by neglect. `store` owns the schema and the statements. `admin`, `artifacts`,
`background`, `backup`, `export`, `extract`, `graph`, `ontology`, `ops`, and `retrieval` each
compose their own statements, because a lane query or a maintenance sweep is easier to read
beside the code it serves than three files away. `config` types its statement hooks with
`sqlalchemy.Select`.

The contract also sets `allow_indirect_imports = true`, so only a direct import is forbidden. A
package in the list may still call something that itself uses SQLAlchemy, which is exactly what
`User.exec` is.

## The test that keeps the split honest

A forbidden contract has the opposite failure mode from an exhaustive layers contract. Adding a
new package does not break it, the package just falls outside both lists and gets to compose SQL
with nobody noticing.

`tests/test_contracts.py` closes that hole. It reads `pyproject.toml`, takes the contract's
source list, unions it with a hardcoded `_SQL_COMPOSING` set of the twelve exceptions, and
asserts two things.

```python
assert sources | _SQL_COMPOSING == packages()
assert not sources & _SQL_COMPOSING
```

The first says the two lists together cover every top-level module actually present in
`src/aizk/`. The second says no package is in both. So a new package fails this test until an
author states, in writing, which side of the SQL line it sits on.

## Where ruff picks up what import-linter cannot see

An import contract sees imports and nothing else. It cannot tell that a transport built a
statement out of symbols it imported for a legitimate reason.

Two ruff overlays cover that gap. `src/aizk/mcp/ruff.toml` and `src/aizk/api/ruff.toml` extend the
package configuration and use `TID251` to ban `sqlmodel.select`, `sqlmodel.Session`, the async
session class, `aizk.store.engine.Session`, and `aizk.store.engine.Database` inside the two
transport packages, each with an error message naming the alternative. Repository-wide, the same
mechanism bans `sqlalchemy.select` in favor of `sqlmodel.select` so every query carries sqlmodel's
more precise `Select` type.

| Check | Command | Catches |
|---|---|---|
| layer stack | `chefe run lint-imports-aizk` | an upward import, an unassigned package |
| SQL contract | `chefe run lint-imports-aizk` | a direct `sqlmodel` or `sqlalchemy` import outside the twelve |
| split coverage | `chefe run test-aizk` | a new package in neither list |
| call sites | `chefe run lint` | statement building or session opening inside a transport |

## Next

<div class="not-content">

- [Design principles](/docs/dev/architecture/principles/) explains the rules behind the contracts.
- [Repository tour](/docs/dev/architecture/repository/) says what each package in the stack does.
- [Style and typing](/docs/dev/contributing/style/) covers the rest of the lint and type gates.

</div>
