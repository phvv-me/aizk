# Contributing

Thanks for helping improve aizk.

The full guides live in the docs and are the authority. This file is the short version.

- [Development setup](https://aizk.phvv.me/docs/dev/contributing/setup/)
- [Testing](https://aizk.phvv.me/docs/dev/contributing/testing/)
- [Style and typing](https://aizk.phvv.me/docs/dev/contributing/style/)
- [Releasing](https://aizk.phvv.me/docs/dev/contributing/release/)

## Setup

`chefe` owns dependencies, environments and tasks. Never call `uv run`, `pip`, `python`, `pytest`
or `pixi` directly, because that environment is not the one the gate uses.

```sh
uv tool install "chefe>=0.0.25"
chefe install
```

The manifest is `pyproject.toml`, which holds `[project.dependencies]`, `[dependency-groups].dev`
and the `[tool.chefe]` table that chefe compiles into `.chefe/pixi.toml`.

## Before a pull request

Run the same four gates CI runs.

```sh
chefe run lint            # ruff check + ruff format --check
chefe run lint-imports    # the layered and SQL import contracts (import-linter)
chefe run typecheck       # pyrefly, ty and mypy --strict
chefe run test            # the suite, 100% statement and branch coverage across aizk and eval
```

From the monorepo root the same tasks carry an `-aizk` suffix, so `chefe run test-aizk-cov`,
`chefe run typecheck-aizk` and `chefe run lint-imports-aizk`.

Notes worth knowing before your first run.

- Tests need a reachable PostgreSQL with the VectorChord extensions. The Compose `db` service is
  the shortest path, and `cp src/deploy/.env.example .env` comes first. Database tests skip when
  nothing is reachable.
- Each pytest process creates and drops its own `aizk_test_<pid>` database, so parallel and focused
  runs never collide.
- Coverage is gated at 100 percent statement and branch. `chefe run test-aizk-cov` runs the two
  passes the gate needs and reports the union.
- Model lanes are faked in the suite, so no GPU is required. `chefe run test-aizk-artifact-stack`
  is the separate integration run against the real services inside the Compose network.

Keep changes focused. If the change affects users, update `README.md`, `docs/` and `CHANGELOG.md`
in the same commit.
