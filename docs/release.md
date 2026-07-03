# Release

Releases are driven by the `version` in `pyproject.toml`. Bumping it and merging to
`main` is the whole release. CI runs the gate, and if `v<version>` is not yet a tag,
the workflow builds, publishes to PyPI, tags the commit, and cuts a GitHub release.
An unchanged version is a no-op, so ordinary commits never publish.

## Checklist

1. Bump `version` in `pyproject.toml`.
2. Update `CHANGELOG.md`.
3. Run the local checks.
4. Merge to `main`. `.github/workflows/publish.yml` builds, publishes, and tags.
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
