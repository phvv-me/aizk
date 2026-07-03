# Contributing

Thanks for helping improve aizk.

## Before a Pull Request

- Install dependencies with `uv sync --extra dev`.
- Run `uv run ruff check . && uv run ruff format --check .`.
- Run `uv run mypy src && uv run pyrefly check`.
- Run `uv run pytest -q`.

Keep changes focused. If the change affects users, update `README.md`, `docs/`, and `CHANGELOG.md`.
