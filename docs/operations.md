# Operations

This page explains how to run Aizk as durable shared infrastructure. It covers process
privileges, network exposure, PostgreSQL storage, encryption, backups, upgrades, and the
five-second health check. The [security page](security.md) defines the threat model and release
gate.

## Deployment shape

One Compose file runs several processes from one Aizk image. The split keeps public request
handling away from database-owner credentials without creating a second deployment definition.

| Service | Responsibility | Database authority | Public listener |
| --- | --- | --- | --- |
| `volume-init` | Prepare writable OAuth and backup volumes | None | None |
| `db` | PostgreSQL 18 with VectorChord | Cluster owner | Loopback only |
| `setup` | Apply migrations and install PgQueuer | Cluster owner | None |
| `server` | Serve the four MCP tools | Forced-RLS app role | Loopback only |
| `worker` | Run projections, maintenance, and backups | App role plus owner maintenance | None |
| `logto` | Own users, organizations, roles, and OAuth login | Dedicated Logto database role | Loopback only |
| model services | Embed, extract, rerank, and gate | None | Loopback only |
| `cloudflared` | Carry authenticated traffic to the Cloudflare edge | None | Outbound only |

The `server` explicitly receives an empty owner password and owner URL. If request handling is
compromised, that process still reaches tenant data only through the `aizk_app` role and forced
PostgreSQL row security. The private `worker` retains owner access because scope discovery,
backups, and schema maintenance require it.

Long-lived Aizk runtime containers use an unprivileged user, a read-only root filesystem, no Linux
capabilities, `no-new-privileges`, a bounded process count, and a private temporary filesystem.
The one-shot `volume-init` service runs as root with only `CAP_CHOWN` and `CAP_FOWNER`, prepares
mode `0700` directories for UID `10001`, and exits before either long-lived process starts.

## First start

Copy the example environment and replace every blank secret. Compose refuses to start while any
database password remains blank.

```sh
cp deploy/.env.example .env
openssl rand -base64 32
```

Use independent random values for these settings.

```sh
AIZK_ADMIN_PASSWORD=
AIZK_APP_PASSWORD=
AIZK_LOGTO_DB_PASSWORD=
```

The migration owner, the forced-RLS application role, and Logto must never share a password.
Start the local stack from the package root.

```sh
docker compose --env-file .env -f deploy/docker-compose.yml up -d
```

The optional public profile also starts Logto, Cloudflare Tunnel, and the authentication
preflight. The tunnel must publish Logto's canonical issuer before discovery can succeed. The MCP
server remains stopped until that tunnel is healthy and the preflight validates the Logto and
OAuth configuration with `AIZK_REQUIRE_AUTH=1`.

```sh
docker compose --profile public --env-file .env -f deploy/docker-compose.yml up -d
```

## Network boundary

PostgreSQL, Logto, the Logto admin console, MCP, and every model endpoint bind only to
`127.0.0.1` on the host. Docker warns that a published port without a host address binds every
host interface, which is why every mapping is explicit in this file. The Cloudflare container
reaches `server:8080` over the Compose network and needs no public inbound host port. See the
[Docker Compose port reference](https://docs.docker.com/reference/compose-file/services/#ports).

PostgreSQL TLS is off inside the single-host Compose network. This is acceptable only while the
database remains loopback-bound and every client runs on the same trusted host. A future remote
database must use certificate-verified TLS and an app URL with `sslmode=verify-full`.

## Crimson storage layout

Crimson was measured on July 15, 2026. It has three SSDs.

| Device | Capacity | Current role |
| --- | --- | --- |
| Samsung 980 PRO NVMe | 1 TB | Ubuntu, Docker, containers, and model cache |
| WD Blue SN580 NVMe | 2 TB | Reserved only for PostgreSQL |
| Crucial MX500 SATA SSD | 2 TB | Datasets, checkpoint overflow, and local backup staging |

The second NVMe is mounted at `/mnt/ssd2`. Aizk must be its only workload. Crimson uses this
setting after the live database has been migrated.

```sh
AIZK_POSTGRES_DATA_VOLUME=/mnt/ssd2/postgres
```

Compose accepts either a Docker volume name or an absolute host directory. The portable default
remains `db-data`. Never change the setting on a populated deployment until a verified backup
exists and the old database is stopped. The PostgreSQL process in the pinned image uses UID and
GID `999`, so the host directory must be owned by `999:999` with mode `0700`.

The current Crimson databases total less than 60 MB. A controlled rebuild onto the dedicated
device is safer than moving a live Docker volume in place. The migration must preserve separate
archives for Aizk and Logto, initialize the new cluster, restore Logto under its dedicated role,
install the current squashed Aizk schema, restore source data, rebuild projections, run the RLS
lint, and finish with the health probe. Do not remove the old Docker volume until a restore drill
and end-to-end recall both pass.

## Encryption at rest

PostgreSQL does not provide transparent cluster encryption in core. Its documentation recommends
filesystem or block encryption when protection from a stolen drive is required. On Linux, LUKS2
over dm-crypt is the appropriate boundary. Column encryption with `pgcrypto` is not a replacement
for Aizk because embeddings, lexical indexes, graph traversal, and reranking require searchable
plaintext inside the trusted database process. See the [PostgreSQL encryption
options](https://www.postgresql.org/docs/18/encryption-options.html).

Crimson currently has no TPM device available to systemd. LUKS therefore needs one of two honest
unlock designs.

- A passphrase entered after reboot gives the strongest simple protection but requires an
  operator during recovery.
- A network-bound key from a separate trusted machine supports unattended reboot but adds a key
  service and a recovery dependency.

Storing the LUKS key on Crimson's unencrypted root disk protects only against removal of the
database SSD. It does not protect against theft of the whole machine and should not be described
as full at-rest encryption. Until an unlock design is chosen, the dedicated NVMe remains
unencrypted and this limitation stays visible in the security checklist.

Backups require independent encryption even after the live data device uses LUKS. A copied dump
leaves the mounted filesystem and must protect itself at the remote destination.

## PostgreSQL initialization and tuning

PostgreSQL 18 enables page checksums by default. Compose also passes `--data-checksums` explicitly
so the desired initialization is visible. Checksums detect corrupted database pages when they
are read. They do not cover every internal file and do not replace backups. Check the live state
with the following command. See the [PostgreSQL checksum
documentation](https://www.postgresql.org/docs/18/checksums.html).

```sh
docker compose --env-file .env -f deploy/docker-compose.yml exec -T db \
  psql -U aizk_admin -d aizk -Atc "SHOW data_checksums"
```

The committed defaults target Crimson's 256 GB RAM and dedicated NVMe. Smaller hosts must lower
the memory settings before startup.

| Setting | Crimson default | Reason |
| --- | --- | --- |
| `shared_buffers` | 16 GB | Keep the active graph and indexes warm without displacing the OS cache |
| `effective_cache_size` | 128 GB | Tell the planner how much shared and filesystem cache is realistically reusable |
| `work_mem` | 16 MB | Support bounded sorts while limiting multiplication across concurrent plans |
| `maintenance_work_mem` | 2 GB | Accelerate vacuum and index maintenance |
| `effective_io_concurrency` | 200 | Model an NVMe device rather than a rotating disk |
| `maintenance_io_concurrency` | 200 | Let maintenance exploit the same storage parallelism |
| `random_page_cost` | 1.1 | Keep random NVMe reads close to sequential read cost |
| `checkpoint_timeout` | 15 minutes | Spread checkpoint writes over a longer interval |
| `max_wal_size` | 8 GB | Reduce forced checkpoints during graph rebuilds |
| `wal_compression` | on | Reduce full-page-image WAL with available CPU headroom |
| `track_io_timing` | on | Make database I/O visible in operational diagnostics |
| `log_lock_waits` | on | Record lock stalls before they become unexplained queue lag |
| `log_min_duration_statement` | 1 second | Capture slow statements without logging normal recall traffic |

`effective_cache_size` is only a planner estimate. It does not reserve memory. PostgreSQL advises
against assigning most RAM to `shared_buffers` because it also relies on the operating system
cache. See the [resource configuration
reference](https://www.postgresql.org/docs/18/runtime-config-resource.html) and [query planner
reference](https://www.postgresql.org/docs/18/runtime-config-query.html).

The WAL settings favor a write-heavy projection pass while preserving crash safety. Larger WAL
and longer checkpoints trade disk space and recovery time for smoother I/O. See the [WAL
configuration reference](https://www.postgresql.org/docs/18/runtime-config-wal.html).

Treat these values as an initial measured operating point. Review `pg_stat_statements`, cache hit
rate, checkpoint frequency, temporary file volume, queue lag, and the five-second health report
after realistic ingestion. Change one group at a time and keep the previous value in the decision
record.

## Backups and recovery

The private worker runs a scheduled custom-format `pg_dump`. Each archive contains the complete
Aizk schema, ontology, tenant rows, and temporal history. The backup code keeps the database
password out of process arguments and creates every archive with mode `0600`.

```sh
AIZK_BACKUP_ENABLED=1
AIZK_BACKUP_DIR=/backups
AIZK_BACKUP_CRON="0 2 * * *"
AIZK_BACKUP_KEEP_DAYS=14
```

The local backup volume is recovery staging, not a complete backup strategy. It shares Crimson's
power, controller, administrator account, and physical location. Copy every successful dump to an
encrypted destination on another machine or object store. Keep at least one older generation
outside the normal retention window. Alert when replication fails.

Run an on-demand Aizk backup inside the private worker.

```sh
docker compose --env-file .env -f deploy/docker-compose.yml exec -T worker \
  aizk db backup /backups/aizk-$(date +%F).dump
```

Logto is a separate database and needs a separate archive. An Aizk dump does not contain Logto
accounts, organization membership, OAuth clients, or consent records.

```sh
docker compose --env-file .env -f deploy/docker-compose.yml exec -T db \
  pg_dump -U aizk_admin --format=custom --file=/tmp/logto.dump logto
```

Move that archive out of the container immediately and encrypt it with the external backup set.
Perform a scratch restore every month. A backup is trusted only after PostgreSQL accepts it, the
schema and RLS checks pass, Logto starts, and a real authenticated recall succeeds.

## SSD health and capacity

Install the host tools once, then enable continuous SMART monitoring and periodic TRIM.

```sh
sudo apt install smartmontools nvme-cli
sudo systemctl enable --now smartmontools
sudo systemctl enable --now fstrim.timer
```

Review the dedicated device and both other SSDs.

```sh
sudo smartctl -a /dev/nvme1n1
sudo smartctl -a /dev/nvme0n1
sudo smartctl -a /dev/sda
```

Alert on critical warnings, media errors, increasing error logs, spare below threshold,
temperature above the vendor operating range, or percentage used approaching the planned service
life. Run short self-tests weekly and long self-tests monthly where the device supports them.

Keep at least 20 percent of `/mnt/ssd2` free. Warn at 75 percent and stop nonessential ingestion at
80 percent until capacity is understood. PostgreSQL needs space for WAL spikes, index builds,
vacuum rewrites, and restore work in addition to table size.

## Five-second health overview

The bounded health command checks the migration head, forced RLS, principal table counts, queue
failures, served model aliases, model context, per-scope graph progress, recent writes, and one
real recall. Database and model probes run concurrently and recall has a 3.5 second timeout.

```sh
docker compose --env-file .env -f deploy/docker-compose.yml exec -T worker aizk db health
```

Run this owner-level diagnostic in `worker`, never `server`. The public process intentionally has
no migration-owner credential, so a compromised MCP request path cannot turn the health command
into an RLS bypass.

A healthy public deployment reports an up-to-date migration, no RLS violations, Logto identity
mode, reachable and correctly named models, no retained queue failures, processed chunks catching
up with stored chunks, and a nonempty recall sample without an error.

## Authentication and upgrades

A public deployment uses Logto as the only identity and organization authority. FastMCP exposes
an OAuth proxy to MCP clients while Logto handles human login. The committed public preflight
requires the public MCP URL, Logto URL, Logto Management API client, OAuth web client, and their
secrets. Client-specific setup lives in [MCP clients](mcp-clients.md), and the complete authority
model lives in [Identity and sharing](engine/identity.md).

Every external image uses a reviewed version tag. VectorChord Suite currently provides only the
floating `pg18-latest` suite tag, so that one image also carries the tested digest. Update images
deliberately, review release notes, take both database backups, rebuild, run migrations through the
one-shot setup service, and finish with the full health probe. Never use `down -v` during an
upgrade.
