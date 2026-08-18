---
title: "Upgrades"
description: "Moving a running deployment forward without losing memory or sessions."
---

Upgrading aizk is mostly boring, which is the goal. The two things that surprise people are that
the server runs source baked into its image and that rotating one particular secret logs everyone
out. This page covers both. It assumes the service list from
[Deployment topology](/docs/dev/run/topology/) and a working backup routine from
[Backups and recovery](/docs/dev/run/backups/).

```text
  what changed?
    aizk source, web, docs  ─▶ build the image ─▶ up -d ─┐
    an .env value ──────────▶ up -d recreates affected ──┤
    a pinned image tag ─────▶ pull ─▶ up -d ─────────────┼─▶ setup migrates ─▶ admin health
    nothing, a hung process ─▶ restart ──────────────────────────────────────▶ admin health
```

## Rebuild, do not restart

:::caution[A restart runs the old code]
`src/deploy/Dockerfile` copies `src` into the image and installs the project into a virtualenv
built at image time. There is no bind mount of the source and no editable install at runtime, so
`docker compose restart` runs exactly the code that was already there.
:::

Any change to Python source, to the SvelteKit app or to these documentation pages needs the image
rebuilt and the container recreated.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml build server worker
docker compose --env-file .env -f src/deploy/docker-compose.yml up -d
```

`up -d` recreates only what actually changed, so this is safe to run on the whole project. The
same applies to `frontend` and `docs`, which are separate build targets in the same Dockerfile.

`patos` and `rlsalchemy` arrive from PyPI at the exact versions `pyproject.toml` pins. The reviewed
SQLAlchemy row security fork is fixed to one public revision and installed by the same bootstrap
script CI runs. `uv.lock` fixes the rest of the graph. A build therefore never resolves a sibling
working tree or an unreviewed package revision by accident.

## Image pinning

External images fall into two groups. `db`, `objects`, `clamav` and `docling` carry a validated
release tag plus a tested digest, so the exact bytes are fixed. VectorChord Suite only publishes
a floating `pg18-latest` suite tag, which is precisely why its digest is pinned.

The rest carry a version tag alone, which are `vllm/vllm-openai:v0.26.0`, `svhd/logto:1.41.0`,
`cloudflare/cloudflared:2026.7.3`, `grafana/loki:3.7.5`, `grafana/tempo:2.9.4`,
`grafana/alloy:v1.16.1`, `grafana/grafana:13.1.2` and `caddy:2.10.2-alpine`.

Moving any of them is a deliberate change. Read the upstream release notes, take both database
archives and an object-store copy, pull, rebuild, and finish with the full health probe.

## Migrations run in one place

`setup` is a one-shot service running `admin database setup`, which upgrades Alembic to head and
installs the PgQueuer schema. It holds the owner credential and exits. `server`, `api` and
`worker` all declare `service_completed_successfully` on it, so no request path ever starts
against an older schema than the one it was built for.

The PostgreSQL history lives in `src/aizk/store/migrations/versions/`. The CockroachDB profile uses
one fused baseline in
`src/aizk/store/migrations/cockroachdb/versions/`. [Migrations and DDL](/docs/dev/store/migrations/)
explains why the cloud profile starts from one migration rather than replaying PostgreSQL history.

:::danger[Never `down -v` during an upgrade]
It removes the named volumes, which takes the database, the object store, and the ClamAV signatures
with it. It is also the only way to make PostgreSQL re-run `initdb/roles.sh`,
so reach for it only when that is exactly what you want.
:::

## Rotating secrets

Rotating a database password means updating the role and the deployment secret in one maintenance
window, then recreating only the services that use that role. `initdb/roles.sh` is idempotent and
reconciles every role with the current `.env`, so it is the tool for the database half. If you
replaced that file through an rsync deployment, recreate the `db` container before running it,
because a live bind mount keeps the old inode.

The web session secret is separate and independent by validation. `Settings` rejects it when it
is shorter than 32 bytes or equal to the web or Management API client secret.

## The order that works

Take both database archives and an object-store copy. Pull and rebuild. Bring the stack up so
`setup` migrates. Run `aizk admin health` in `worker` and confirm the migration is at head, RLS
reports no violations, the model endpoints match their configured aliases and the real find
returns candidates. Only then restore public traffic.

Upstream of all that, the same gate runs in CI on every pull request. `.github/workflows/ci.yml`
installs the frozen lock, then runs Ruff, import-linter, all three type checkers, and pytest against
a real VectorChord database with the same restricted app role, so forced row security is
exercised the way production has it. `.github/workflows/docs.yml` builds this site and runs the
page gate. A change that has not passed both should not reach a deployment.

## Next

<div class="not-content">

- [The release gate](/docs/dev/run/release-gate/) is the checklist for the last step above.
- [Backups and recovery](/docs/dev/run/backups/) covers the archives this depends on.
- [Migrations and DDL](/docs/dev/store/migrations/) explains the schema side.
- [Testing](/docs/dev/contributing/testing/) explains what the CI gate actually proves.

</div>
