# The aizk image, one image for the server, the worker, and the backup, since `serve-mcp` gathers
# the worker on its own event loop and the backup is a scheduled task that worker fires.
#
# Multi-stage: the builder carries a C toolchain because on Python 3.14 a few dependencies
# (asyncpg among them) ship no wheel yet and compile from source, while the runtime stays slim,
# copying only the built virtualenv and the source it points at, plus the Postgres 18 client.

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder
# build-essential for the source-only wheels, git for the rls fork's direct reference
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# dependency layer, cached across source edits: install everything the manifest names but not the
# project itself, so touching src never re-resolves torch and the rest.
COPY pyproject.toml README.md ./
RUN uv sync --no-dev --no-install-project
# the project over the cached deps, then the rls fork overlay last so the sync never reverts it,
# since the bare `rls` PyPI name is the upstream base the fork reworked. Both the overlay and the
# sqlalchemy 2.1 override (pyproject's [tool.uv]) disappear once the fork ships as `rlsalchemy`.
COPY src ./src
RUN uv sync --no-dev \
    && uv pip install --python .venv --reinstall "rls @ git+https://github.com/phvv-me/rls"

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
# postgresql-client-18, version-matched to the vchord pg18 server, so `aizk backup`/`restore` run
# right inside this container against the db service with `pg_client_launcher` left empty, no
# docker-exec-into-a-sibling gymnastics. The PGDG apt repo carries the 18 client the base's own
# older client would refuse to dump an 18 server with.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# the built virtualenv and the source its editable install points at, both at the same path the
# builder used so the venv's own path references still resolve
COPY --from=builder /app/.venv /app/.venv
COPY pyproject.toml README.md ./
COPY src ./src
# `--no-sync` so the entrypoint never re-resolves at runtime, it runs the environment built above
ENTRYPOINT ["uv", "run", "--no-sync", "aizk"]
CMD ["serve-mcp"]
