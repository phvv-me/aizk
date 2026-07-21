---
title: "PostgreSQL and storage"
description: "Initialization, the tuning that assumes a large host, and the disk layout."
---

PostgreSQL is not a dependency of aizk so much as it is the engine. Vectors, BM25, the graph, the
job queue and every authorization decision live in it. This page covers how the cluster is
created, tuned and stored. It assumes you know the service list from
[Deployment topology](/docs/dev/run/topology/) and can read SQL.

## Initialization creates three roles

The `db` service starts with `POSTGRES_INITDB_ARGS: --data-checksums` and mounts
`src/deploy/initdb/roles.sh` into `/docker-entrypoint-initdb.d/`. PostgreSQL runs that script
exactly once, against an empty data directory, before any migration connects.

```text
  aizk_admin ──owns──▶ aizk database ──▶ every table, bypasses RLS
       │
       └──creates──▶ aizk_app   NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
       │                  └──▶ SELECT/INSERT/UPDATE/DELETE by default privilege
       └──creates──▶ logto     ──owns──▶ logto database, a separate database
```

`aizk_admin` is a fixed literal in the Compose file rather than a variable, because
`Settings.admin_database_url` hardcodes the same name and only the password travels through the
environment. `aizk_app` is the role every request path uses, and it can neither bypass row
security nor own a table, which is the whole point of
[Row level security](/docs/dev/store/rls/). `logto` owns only the separate `logto` database.

The script is idempotent, so run it again after a role restore or a secret rotation to replace
archived password hashes with the current `.env` values.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml exec -T db \
  /docker-entrypoint-initdb.d/roles.sh
```

The healthcheck authenticates over TCP with the current project secret rather than using
`pg_isready`, so password drift shows up as an unhealthy container instead of a mystery later.

## Extensions and preloaded libraries

The image is `tensorchord/vchord-suite:pg18-latest`, pinned by digest. Compose replaces its `CMD`
outright, so the suite's own settings are repeated verbatim alongside ours.

```
shared_preload_libraries=vchord,vchord_bm25,vector,pg_tokenizer,pg_stat_statements
search_path="$user", public, bm25_catalog, tokenizer_catalog
app.scopes=
```

`pg_stat_statements` rides along so query statistics come from the catalog view rather than an
ad-hoc `EXPLAIN ANALYZE`, and `admin database setup` runs the matching
`CREATE EXTENSION IF NOT EXISTS`. A preloaded library only takes effect on the next server start.

`app.scopes` is the request context. Its empty server default is deliberate, because a session
that has not bound a caller sees nothing rather than everything.

## Tuning assumes a 256 GB host

These are the committed defaults, each overridable through the matching `AIZK_PG_` variable.

| Setting | Default | Why |
|---|---|---|
| `shared_buffers` | 16GB | keep the active graph and its indexes warm |
| `effective_cache_size` | 128GB | a planner estimate, it reserves nothing |
| `work_mem` | 16MB | bounded sorts without multiplying across plans |
| `maintenance_work_mem` | 2GB | faster vacuum and index builds |
| `effective_io_concurrency` | 200 | model NVMe rather than a rotating disk |
| `maintenance_io_concurrency` | 200 | same parallelism for maintenance |
| `random_page_cost` | 1.1 | random NVMe reads cost near sequential |
| `checkpoint_timeout` | 15min | spread checkpoint writes |
| `max_wal_size` | 8GB | fewer forced checkpoints during graph rebuilds |
| `min_wal_size` | 2GB | keep segments around for reuse |
| `wal_compression` | on | less full-page-image WAL, spends CPU |
| `track_io_timing` | on | make I/O visible in diagnostics |
| `log_lock_waits` | on | catch lock stalls before they read as queue lag |
| `log_min_duration_statement` | 1000ms | slow statements only |
| `autovacuum_vacuum_scale_factor` | 0.05 | vacuum earlier than the default |
| `autovacuum_analyze_scale_factor` | 0.02 | analyze earlier than the default |

A smaller host must lower the memory values before PostgreSQL first starts. Treat all of this as
a measured starting point rather than an answer. After realistic ingestion, look at
`pg_stat_statements`, the cache hit rate, checkpoint frequency, temporary file volume and queue
lag, then change one group at a time.

Confirm checksums are actually on, since they detect corrupted pages when they are read and are
easy to assume rather than verify.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml exec -T db \
  psql -U aizk_admin -d aizk -Atc "SHOW data_checksums"
```

## Storage layout

Every persistent path is a Compose variable that takes either a named volume or an absolute host
directory. The named-volume defaults are fine for development and prove nothing about which
physical disk holds the bytes.

```sh
AIZK_POSTGRES_DATA_VOLUME=/mnt/ssd2/aizk/postgres
AIZK_OBJECT_DATA_VOLUME=/mnt/ssd2/aizk/objects
AIZK_BACKUP_VOLUME=/mnt/ssd2/aizk/backups
AIZK_OAUTH_VOLUME=/mnt/ssd2/aizk/oauth
AIZK_CLAMAV_DATA_VOLUME=/mnt/ssd2/aizk/clamav
AIZK_LOKI_VOLUME=/mnt/ssd2/aizk/loki
AIZK_ALLOY_VOLUME=/mnt/ssd2/aizk/alloy
AIZK_GRAFANA_VOLUME=/mnt/ssd2/aizk/grafana
```

Separate subdirectories keep ownership and backup policy explicit even when one device holds them
all. Ownership is not uniform. The PostgreSQL process in the pinned image runs as UID and GID
`999`, so its host directory must be `999:999` with mode `0700`, while the aizk runtime
directories belong to UID `10001` and the observability directories belong to Loki, Alloy and
Grafana separately. Each one needs the UID its own image uses.

Note that the mount point is `/var/lib/postgresql` and not the data directory inside it, because
PostgreSQL 18 images store data under a major-version subdirectory.

:::danger[Never move a populated service path casually]
Change one only when a verified backup exists and the service is stopped. Moving a path means
preserving both database archives, stopping only that service, copying with ownership intact,
recreating the service, and then verifying with the RLS check, a restore drill and an
authenticated recall before you remove the old volume.
:::

## Encryption at rest, honestly

Core PostgreSQL has no transparent cluster encryption, and its own documentation points at
filesystem or block encryption when a stolen drive is the threat. On Linux that means LUKS2 over
dm-crypt. Column encryption with `pgcrypto` is not a substitute here, because embeddings, BM25
indexes, graph traversal and reranking all need searchable plaintext inside the database process.

The reference host has no TPM available to systemd, which leaves two honest unlock designs. A
passphrase entered after reboot is the strongest simple option and needs an operator present. A
network-bound key from a separate trusted machine allows unattended reboot and adds a key service
and a recovery dependency.

Storing the LUKS key on the same machine's unencrypted root disk protects against removal of the
database SSD and nothing else. It is not full at-rest encryption and should not be described as
such.

:::caution[The dedicated device ships unencrypted]
Until an unlock design is chosen the database device stays unencrypted, and that stays on
[the release gate](/docs/dev/run/release-gate/) as an accepted gap rather than quietly closing.
:::

## Next

<div class="not-content">

- [Backups and recovery](/docs/dev/run/backups/) covers dumps, restores and the real gaps.
- [Row level security](/docs/dev/store/rls/) explains what `aizk_app` can and cannot see.
- [Migrations and DDL](/docs/dev/store/migrations/) explains how the schema gets created.
- [Hardware and cost](/docs/dev/run/hardware/) sizes the host these defaults assume.

</div>
