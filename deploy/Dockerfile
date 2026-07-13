# The aizk image, one image for the server, the worker, and the backup, since `serve-mcp` gathers
# the worker on its own event loop and the backup is a scheduled task that worker fires.
#
# Multi-stage: the builder carries a C toolchain because on Python 3.14 a few dependencies
# (asyncpg among them) ship no wheel yet and compile from source, while the runtime stays slim,
# copying only the built virtualenv and the source it points at, plus the Postgres 18 client.

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder
# build-essential for the source-only wheels (asyncpg and friends ship no py3.14 wheel yet)
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# Dependency layer, cached across source edits. The project and local RLS package are installed as
# small runtime overlays so changing either never rewrites the multi-gigabyte dependency layer.
COPY pyproject.toml README.md ./
RUN uv sync --no-dev --no-install-project

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
ENV PATH="/app/.venv/bin:$PATH"
# the built virtualenv and the source its editable install points at, both at the same path the
# builder used so the venv's own path references still resolve
COPY --from=builder /app/.venv /app/.venv
COPY pyproject.toml README.md ./
COPY src ./src
# Migrations run against the RLS and Patos APIs from this checkout. These pure Python projects land
# after the stable dependency layer and need no dependency resolution of their own.
COPY --from=patos pyproject.toml README.md /tmp/patos/
COPY --from=patos src /tmp/patos/src
COPY --from=rls pyproject.toml README.md /tmp/rls/
COPY --from=rls rls /tmp/rls/rls
RUN uv pip install --python .venv --reinstall --no-deps /tmp/patos \
    && uv pip install --python .venv --reinstall --no-deps /tmp/rls \
    && uv pip install --python .venv --reinstall --no-deps .
# The built environment is immutable at runtime, so the command needs no resolver wrapper.
ENTRYPOINT ["aizk"]
CMD ["serve-mcp"]
