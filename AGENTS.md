# aizk

A self-hosted multi-tenant memory engine that turns a Zettelkasten into scoped agent-queryable memory over MCP

## Working Rules

- Keep the public surface small and documented.
- Prefer one boring command per task: install, lint, typecheck, test, build, docs, publish.
- Update `README.md`, `CHANGELOG.md`, and `docs/` when behavior changes (`llms.txt` is generated from the docs build).
- Do not add stack details to the README unless users need them to install or run the project.
- Do not commit, tag, publish, or push unless explicitly asked.

## Commands

The dev environment is managed by [uv](https://docs.astral.sh/uv/).

- Install: `uv sync --extra dev`
- Lint: `uv run ruff check . && uv run ruff format --check .`
- Typecheck: `uv run mypy src && uv run pyrefly check`
- Test: `uv run pytest -q`
- Docs: `uv run --extra docs mkdocs build -d site`
- Build: `uv build`
