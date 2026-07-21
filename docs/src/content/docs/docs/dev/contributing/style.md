---
title: "Style and typing"
description: "The code conventions this repository enforces automatically."
---

This page describes what a machine checks, not what somebody prefers. Everything below fails a
build if you break it, so a review here is about design rather than about formatting. It assumes
you can run the tasks from [Development setup](/docs/dev/contributing/setup/).

```text
  chefe run lint            ruff check  +  ruff format --check   (plus pre-commit hygiene)
  chefe run lint-imports    the two import-linter contracts
  chefe run typecheck       pyrefly  ─▶  ty  ─▶  mypy --strict
  chefe run test            pytest, 100% statement and branch
        │
        └── any one of these red means the change does not land
```

## ruff

The configuration is small on purpose. Lines are 99 characters, the target is `py314`, and the
selected rule families are `E`, `F`, `I`, `UP`, `B`, `SIM`, and `TID251`. That gives pycodestyle
and pyflakes for the basics, isort for import order with `aizk`, `eval`, and `alembic` as first
party, pyupgrade so the codebase keeps moving forward with the language, bugbear for the real
mistakes, and flake8-simplify for the ones that make code harder to read than it needs to be.

`TID251` is the interesting one, and it exists to hold a single rule.

```toml
[tool.ruff.lint.flake8-tidy-imports.banned-api]
"sqlalchemy.select".msg = "Use sqlmodel.select for every query."
```

Formatting is `ruff format` and it is checked rather than applied in the gate, so run
`chefe run lint` before you push. At the monorepo level the same task also runs the pre-commit
stack, which adds trailing whitespace and end-of-file fixes, YAML and TOML validation, a large-file
guard, private key detection, a merge-conflict check, `codespell`, and a `lizard` complexity budget.

## The SQLModel boundary

Two rules keep SQL where it belongs, and they are enforced from different directions.

The first is the ruff ban above. Every query in aizk is built with `sqlmodel.select`, never
`sqlalchemy.select`. The reason is concrete rather than stylistic. A statement handed to
`AsyncSession.exec` has to carry sqlmodel's `Select` type for the overloads to resolve, and mixing
the two produced eleven real type mismatches that a version pin was once hiding.

The second is an import-linter contract. Most packages are forbidden from importing `sqlmodel` or
`sqlalchemy` at all, indirectly included, and they reach the database through model classmethods
and `User.exec` instead. The exceptions are exactly the packages that own statements beside the
code they serve, which are `store`, `admin`, `artifacts`, `background`, `backup`, `export`,
`extract`, `graph`, `ontology`, `ops`, `retrieval`, and `config`. That exception list is not
allowed to rot, because `tests/test_contracts.py` asserts the contract's source list plus the
documented exceptions still covers every package that exists.

## Layers

The other import-linter contract places every top-level `aizk` package in one enforced layer, and
it is declared `exhaustive`, so adding a new package breaks the gate until somebody assigns it a
layer deliberately. [Layers and import contracts](/docs/dev/architecture/layers/) explains the
layering itself and why each package sits where it does. Both contracts run under
`chefe run lint-imports-aizk`, with external packages included and type-checking-only imports
excluded.

## Three type checkers

pyrefly, ty, and mypy all run, and each one is there because it catches something the others miss.

| Checker | Covers | Notes |
|---|---|---|
| pyrefly | `src/aizk`, `src/eval`, `tests` | the only one that also checks the test suite |
| ty | `src/aizk`, `src/eval` | two file-scoped overrides, everything else stays an error |
| mypy | `src/aizk`, `src/eval` | `strict`, with two documented relaxations |

Every relaxation is a third-party typing gap and none of them is a shortcut around our own code.
`ty` downgrades `invalid-assignment` on `src/aizk/store/mixins/base.py` alone, because sqlmodel's
config class does not declare Pydantic's `ignored_types` field that SQLAlchemy hybrid properties
need. mypy keeps `implicit_reexport` on, because aizk packages re-export through `__init__` on
purpose and pyrefly and ty already enforce their own view of that, and it disables `call-arg`,
because a `table=True` SQLModel synthesizes its Pydantic `__init__` through the metaclass at
runtime and mypy has no working sqlmodel plugin, so it rejects every keyword construction in the
codebase. Both are irreducible, and every other strict check stays on so a genuine mistake still
fails.

pyright is deliberately not a fourth gate.
[Rejected and deferred](/docs/dev/prior-art/rejected/) records why.

## Never annotate with `object`

`object` is not a type, it is a way of avoiding one. Use a `Protocol` when you need a shape, a
concrete class when you know it, and a `TYPE_CHECKING` import when the only obstacle is a circular
dependency. `typing.Any` is allowed only as a temporary scaffold during development and never in a
change that lands. The monorepo runs a pre-commit hook that greps for the pattern across the
research tree, and inside aizk the rule is held by review plus the three checkers, which have
little to say about an `object` but a great deal to say about the first attribute access on one.

## Suppressions have to be earned

There are currently zero `# type: ignore`, zero `noqa`, and zero `pyrefly: ignore` comments in
`src/`. That is the standard, not a coincidence, and it is worth keeping because it means an error
report is always about the code rather than about the annotations somebody silenced.

Fix the root cause first. If you exhaust the proper fixes and the remaining problem is genuinely a
third-party stub gap, a single narrow `pyrefly: ignore` with a comment explaining which stub is
wrong is acceptable. A bare `noqa` with no reason is not, and neither is widening a signature to
make a checker quiet.

## Conventions the tools cannot check

A few things stay a matter of review. Imports go at the top of the file and prefer relative form
inside the package. String enums use `StrEnum` with `auto()` whenever the member name already is
the wire value. Docstrings are compact, with `param: description` lines and no `Args` or `Returns`
headers, and modules carry no file-level docstring at all. Follow EAFP, keep `try` blocks small,
and never write a bare `except` outside a CLI entry point.
[Design principles](/docs/dev/architecture/principles/) covers the larger structural rules.

## Next

<div class="not-content">

- [Layers and import contracts](/docs/dev/architecture/layers/) is the contract this page points at.
- [Testing](/docs/dev/contributing/testing/) covers the fourth gate, the suite and its coverage floor.
- [Releasing](/docs/dev/contributing/release/) is what happens once all four are green.

</div>
