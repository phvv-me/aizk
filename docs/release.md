# Release

Releases are driven by the `version` in `pyproject.toml`. Bumping it and merging to
`main` is normally the whole release, CI runs the gate, and if `v<version>` is not yet
a tag, the workflow builds, publishes to PyPI, tags the commit, and cuts a GitHub
release. An unchanged version is a no-op, so ordinary commits never publish.

The publish step is presently manual (`workflow_dispatch` only, run from the Actions
tab or `gh workflow run publish.yml`), because the `rls` dependency is a direct git
reference and the pinned SQLAlchemy 2.1 beta needs a `uv` override plain `pip` cannot
express, both block an unattended PyPI upload. Every push to `main` still runs the
full CI gate above regardless.

## Checklist

1. Bump `version` in `pyproject.toml`.
2. Update `CHANGELOG.md`.
3. Run the local checks.
4. Merge to `main`, then manually trigger `publish.yml` to build, publish, and tag.
5. Verify the package page and docs site.

## Commands

- Lint: `uv run ruff check . && uv run ruff format --check .`
- Typecheck: `uv run mypy src && uv run pyrefly check`
- Test: `uv run pytest -q`
- Build: `uv build`

## One-time setup

Register a PyPI [trusted publisher](https://docs.pypi.org/trusted-publishers/) for
this repository against the workflow file `publish.yml` and environment `pypi`. The
file stays named `publish.yml` so the publisher binding keeps matching after edits.
