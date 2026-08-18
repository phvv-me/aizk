# Contributing

Thanks for helping improve aizk.

The full guides live in the docs and are the authority. This file is the short version.

- [Development setup](docs/src/content/docs/docs/dev/contributing/setup.md)
- [Testing](docs/src/content/docs/docs/dev/contributing/testing.md)
- [Style and typing](docs/src/content/docs/docs/dev/contributing/style.md)
- [Releasing](docs/src/content/docs/docs/dev/contributing/release.mdx)

## Setup

`pyproject.toml` owns every Python dependency and `uv.lock` records the exact environment CI uses.

```sh
uv sync --frozen --all-groups
sh scripts/install-sqlalchemy.sh
```

The bootstrap script replaces the registry SQLAlchemy wheel with the reviewed row security fork at
one immutable public revision. Every later `uv run` uses `--no-sync` so it cannot replace that wheel.

## Before a pull request

Run the same four gates CI runs.

```sh
uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
uv run --no-sync lint-imports
uv run --no-sync pyrefly check
uv run --no-sync ty check --python .venv --exit-zero-on-warning
uv run --no-sync mypy src/aizk src/eval
uv run --no-sync python -m pytest -n 4 --dist loadscope --benchmark-disable
```

Notes worth knowing before your first run.

- Tests need a reachable PostgreSQL with the VectorChord extensions. The Compose `db` service is
  the shortest path, and `cp src/deploy/.env.example .env` comes first. Database tests skip when
  nothing is reachable.
- Each pytest process creates and drops its own `aizk_test_<pid>` database, so parallel and focused
  runs never collide.
- Coverage is gated at 100 percent statement and branch. CI runs the two
  passes the gate needs and reports the union.
- Model lanes are faked in the suite, so no GPU is required. The `artifact-integration` Compose service
  is the separate integration run against the real services inside the Compose network.

Keep changes focused. If the change affects users, update `README.md`, `docs/` and `CHANGELOG.md`
in the same commit.
