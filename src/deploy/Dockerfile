# The aizk image, one image for the server, the worker, and backups, since the MCP server gathers
# the worker on its own event loop and backup is a scheduled task that worker fires.
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
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra heavy-converter
RUN .venv/bin/docling-tools models download layout tableformer \
    --output-dir /app/docling-models

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS runtime
# postgresql-client-18, version-matched to the vchord pg18 server, so the admin backup and restore
# commands run right inside this container against the db service with no docker-exec-into-a-sibling
# gymnastics. The PGDG apt repo carries the 18 client the base's own
# older client would refuse to dump an 18 server with.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       postgresql-client-18 \
       tesseract-ocr \
       tesseract-ocr-eng \
       tesseract-ocr-jpn \
       tesseract-ocr-jpn-vert \
    && tesseract --list-langs | grep -qx jpn \
    && tesseract --list-langs | grep -qx jpn_vert \
    && groupadd --gid 10001 aizk \
    && useradd --uid 10001 --gid 10001 --create-home aizk \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"
# the built virtualenv and the source its editable install points at, both at the same path the
# builder used so the venv's own path references still resolve
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/docling-models /app/docling-models
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# The house packages are NOT overlaid from sibling checkouts any more. They arrive from PyPI at
# the exact versions `pyproject.toml` pins, through the locked `uv sync` in the builder stage,
# which is the whole point. Reinstalling the working tree here meant the image ran different code
# from the one the test suite resolved, so a green suite said nothing about what shipped, and a
# base class that started rejecting unknown fields reached production having never once executed
# in a test. Editing a sibling checkout no longer changes this image, and that is deliberate.
# Publish the package, bump the pin, then build.
RUN uv pip install --python .venv --reinstall --no-deps . \
    && .venv/bin/python -c "import aizk.cli"
# The built environment is immutable at runtime, so the command needs no resolver wrapper.
USER 10001:10001
ENTRYPOINT ["aizk"]
CMD ["admin", "server", "mcp"]

# Build the SvelteKit interface with pnpm and keep only its production node_modules. The API
# origin and every Logto credential are runtime environment, so the build bakes no deployment
# configuration into the bundle.
FROM node:24-alpine AS web-build
RUN npm install -g pnpm@11
WORKDIR /app
COPY src/web/package.json src/web/pnpm-lock.yaml src/web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY src/web ./
RUN pnpm build && pnpm prune --prod

# The adapter-node server renders every browser page. It runs as the unprivileged node user
# with only the build output and production dependencies, no Python and no database credential.
FROM node:24-alpine AS web
ENV NODE_ENV=production
WORKDIR /app
COPY --from=web-build /app/build ./build
COPY --from=web-build /app/node_modules ./node_modules
COPY --from=web-build /app/package.json ./package.json
COPY --from=web-build /app/start.js ./start.js
USER node
EXPOSE 3000
CMD ["node", "build"]

# The exact production SvelteKit application also runs in the AWS profile. Lambda Web Adapter
# turns Function URL events forwarded by the public Python Lambda into ordinary HTTP requests,
# while the small startup module resolves only the two web secrets assigned to this function.
FROM web AS lambda-web
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.0.0 /lambda-adapter /opt/extensions/lambda-adapter
ENV PORT=8080 \
    AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/healthz \
    AWS_LWA_ENABLE_COMPRESSION=true
EXPOSE 8080
CMD ["node", "start.js"]

# The marketing page and the documentation are one static Astro build. Nothing here reaches the
# database or Logto, so the output is plain files and the serving stage carries no runtime.
FROM node:24-alpine AS docs-build
ARG AIZK_DOCS_SITE_URL=https://aizk.example.com
ARG AIZK_DOCS_MCP_CLIENT_ID=YOUR_AIZK_CLIENT_ID
ENV AIZK_DOCS_SITE_URL=${AIZK_DOCS_SITE_URL}
ENV AIZK_DOCS_MCP_CLIENT_ID=${AIZK_DOCS_MCP_CLIENT_ID}
RUN npm install -g pnpm@11
WORKDIR /app
COPY docs/package.json docs/pnpm-lock.yaml docs/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY docs ./
COPY .agents /.agents
COPY .claude-plugin /.claude-plugin
COPY plugins /plugins
COPY README.md /README.md
COPY SECURITY.md ROADMAP.md CHANGELOG.md pyproject.toml /
COPY infra/aws/README.md /infra/aws/README.md
COPY src/web/.env.example /src/web/.env.example
COPY src/deploy/.env.example src/deploy/Dockerfile src/deploy/docker-compose.yml /src/deploy/
COPY hackathon /hackathon
COPY src/web/static /src/web/static
# Rasterizing SVGs is platform dependent because Alpine and the development environment carry
# different font engines. The committed outputs pass `brand:check` before the image build, while
# this stage consumes those exact files and verifies the rendered pages without regenerating them.
RUN pnpm exec astro build && node scripts/check-pages.mjs

# Static file serving only. Astro emits `/docs/quickstart/index.html`, so try_files resolves the
# directory form, and the content-hashed `_astro` assets are safe to cache for a year.
FROM caddy:2.10.2-alpine AS docs
RUN setcap -r /usr/bin/caddy \
    && addgroup -g 10001 caddy \
    && adduser -D -H -u 10001 -G caddy caddy \
    && chown -R caddy:caddy /config /data
COPY --from=docs-build /app/dist /srv
COPY src/deploy/Caddyfile.docs /etc/caddy/Caddyfile
USER caddy
EXPOSE 8082

# The real integration target adds only the package test dependencies and test tree. It runs inside
# the Compose network against PostgreSQL, SeaweedFS, ClamAV, Docling, and the model services while
# the production runtime remains free of test tools.
FROM runtime AS integration-test
USER root
RUN uv sync --frozen --group dev \
    && uv pip install --python .venv --reinstall --no-deps .
COPY --from=tests . ./tests
USER 10001:10001
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["tests/integration/test_artifact_stack.py", "-m", "integration and artifact_stack", "--no-cov"]

# AIZK exercises the proposed PostgreSQL row security API before it is available in an upstream
# SQLAlchemy release. Keep the release image on the exact revision CI installs.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS sqlalchemy-build
WORKDIR /src
COPY --from=sqlalchemy . /tmp/sqlalchemy
RUN test -f /tmp/sqlalchemy/pyproject.toml \
    && grep -q CreatePolicy /tmp/sqlalchemy/lib/sqlalchemy/dialects/postgresql/__init__.py \
    && DISABLE_SQLALCHEMY_CEXT=1 uvx --from build pyproject-build \
       --wheel --outdir /tmp/sqlalchemy-dist /tmp/sqlalchemy \
    && rm -rf /tmp/sqlalchemy

# The AWS target uses the managed Python runtime interface and the same source and dependency
# graph as the local image. Lambda selects the MCP, worker, or setup handler through an image
# command override, so one immutable digest serves all three functions.
FROM public.ecr.aws/lambda/python:3.14 AS lambda
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR ${LAMBDA_TASK_ROOT}
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY --from=sqlalchemy-build /tmp/sqlalchemy-dist /tmp/sqlalchemy-dist
RUN uv pip uninstall --python .venv sqlalchemy \
    && .venv/bin/python -m zipfile -e /tmp/sqlalchemy-dist/*.whl \
       .venv/lib/python3.14/site-packages \
    && .venv/bin/python -c \
       "from sqlalchemy.dialects.postgresql import CreatePolicy, DropPolicy, Policy" \
    && rm -rf /tmp/sqlalchemy-dist
COPY src ./src
COPY --from=docs-build /app/dist ./static
# SvelteKit content hashes make these assets immutable. Serving them from the public Lambda avoids
# spending a second synchronous Lambda invocation on every browser JavaScript and CSS request.
# The page-rendering image comes from the same `web-build` stage, so its HTML names these exact
# files and the two images cannot drift within one build.
COPY --from=web-build /app/build/client/_app ./static/_app
RUN uv pip install --python .venv --reinstall --no-deps .
ENV PYTHONPATH="${LAMBDA_TASK_ROOT}/.venv/lib/python3.14/site-packages:${LAMBDA_TASK_ROOT}/src"
CMD ["aizk.commands.aws_mcp.mcp_handler"]

# Caddy is the single public origin router for the MCP, OAuth, API, and interface paths. The
# stage strips the binary's file capabilities and runs as an unprivileged caddy user, which the
# upstream image does not create, on its high port with every capability dropped. Its config and
# data homes become caddy-owned for plain runs; under Compose tmpfs mounts cover both paths.
# It contains no Python environment, credential, or Logto secret.
FROM caddy:2.10.2-alpine AS caddy
RUN setcap -r /usr/bin/caddy \
    && addgroup -g 10001 caddy \
    && adduser -D -H -u 10001 -G caddy caddy \
    && chown -R caddy:caddy /config /data
COPY src/deploy/Caddyfile /etc/caddy/Caddyfile
USER caddy
