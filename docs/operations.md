# Operations

This page explains how to run Aizk as durable shared infrastructure. It covers process
privileges, network exposure, PostgreSQL storage, encryption, backups, upgrades, and the
five-second health check. The [security page](security.md) defines the threat model and release
gate.

## Deployment shape

One Compose file runs several processes from one Aizk image. The split keeps public request
handling away from database-owner credentials without creating a second deployment definition.

| Service | Responsibility | Database authority | Host publication |
| --- | --- | --- | --- |
| `volume-init` | Prepare writable OAuth and backup volumes | None | None |
| `db` | PostgreSQL 18 with VectorChord | Cluster owner | None |
| `setup` | Apply migrations and install PgQueuer | Cluster owner | None |
| `server` | Serve the four MCP tools | Forced-RLS app role | None |
| `worker` | Run projections, maintenance, and backups | App role plus owner maintenance | None |
| `logto` | Own users, organizations, roles, and OAuth login | Dedicated Logto database role | None |
| `logto-setup` | Reconcile AIZK-owned Logto authorization policy | None | None |
| `public-check` | Fail closed on incomplete public authentication settings | None | None |
| `web-check` | Fail closed on incomplete browser authentication settings | None | None |
| `api` | Serve the browser JSON API over the shared AIZK memory service | Forced-RLS app role | None |
| `web` | Render the SvelteKit interface and hold the Logto web application | None | None |
| `caddy` | Route the AIZK tunnel origin to the server, capability upload, and interface | None | None |
| `objects` | Store immutable artifact bytes through SeaweedFS S3 | None | None |
| `clamav` | Scan incoming artifact bytes and update malware signatures | None | None |
| `docling` | Convert accepted originals into Markdown and JSON derivatives | None | None |
| model services | Embed, extract, rerank, and gate | None | None |
| `cloudflared` | Carry authenticated traffic to the Cloudflare edge | None | None; outbound only |
| `alloy` | Collect this Compose project's Docker logs | None | None |
| `loki` | Retain and query centralized operational logs | None | None |
| `grafana` | Inspect logs through the provisioned Loki source | None | Loopback only |
| `observability-init` | Prepare the Loki volume for its unprivileged process | None | None |

## Exposure model

The Cloudflare Tunnel is the only Internet ingress. Its `aizk.phvv.me` route exposes the
SvelteKit frontend, `/mcp` and `/mcp/*`, the FastMCP OAuth routes
`/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp`,
`/authorize`, `/token`, `/register`, `/auth/callback`, and `/consent`, and exactly
`PUT /api/uploads/*` for capability redemption. Its `auth.phvv.me` route exposes Logto.
Caddy returns 404 for every other `/api` request. Grafana is outside the tunnel and remains
available only through its `127.0.0.1` host binding.

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
cp src/deploy/.env.example .env
openssl rand -base64 32
```

Use independent random values for these settings.

```sh
AIZK_ADMIN_PASSWORD=
AIZK_APP_PASSWORD=
AIZK_LOGTO_DB_PASSWORD=
AIZK_LOGTO_CLIENT_SECRET=
AIZK_OAUTH_CLIENT_SECRET=
AIZK_WEB_CLIENT_SECRET=
AIZK_WEB_SESSION_SECRET=
AIZK_OBJECT_STORE_ACCESS_KEY=
AIZK_OBJECT_STORE_SECRET_KEY=
AIZK_DOCLING_API_KEY=
```

The migration owner, the forced-RLS application role, Logto, SeaweedFS, and Docling must never
share a password or key. `AIZK_WEB_SESSION_SECRET` must contain at least 32 bytes and must not equal
the web, Management API, or OAuth client secret.
`src/deploy/logto.conf` contains the committed nonsecret authorization policy. Pydantic and Compose
load it before `.env`, so any matching `AIZK_` value in `.env` is the deployment override. Keep
Logto M2M credentials, OAuth application credentials, public URLs, and tunnel tokens only in
`.env`.

The default policy has one global human role named `aizk-user` with the AIZK API `control`
permission. New users receive it because the role is a Logto default. The organization roles are
admin, editor, and viewer. Admin receives `write:memory`, `manage:member`, and `delete:member`.
Editor receives `write:memory`, while viewer remains read-only. The former `invite:member`
permission is retired because AIZK has no invitation workflow. Inspect or repair the live tenant
with these idempotent commands.

```sh
aizk logto audit
aizk logto apply
```

Start the local stack from the package root.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml up -d
```

The optional public profile also starts Logto, Cloudflare Tunnel, authorization reconciliation,
the authentication preflight, and the browser interface. The tunnel must publish Logto's
canonical issuer before discovery and Management API access can succeed. The MCP server remains
stopped until the tunnel is healthy, `logto-setup` has reconciled policy, and the preflight
validates the Logto and OAuth configuration with `AIZK_REQUIRE_AUTH=1`. The browser backend also
waits for `web-check`. Both browser URLs must use the same HTTPS origin. The check also requires
the Logto application and an independent session secret containing at least 32 bytes.

```sh
docker compose --profile public --env-file .env -f src/deploy/docker-compose.yml up -d
```

The browser uses a separate Logto Traditional Web application. Configure its exact redirect URI
as `https://aizk.phvv.me/auth/sign-in-callback`. Keep Logto at `https://auth.phvv.me`. Cloudflare
Tunnel maps `auth.phvv.me` to `logto:3001` and maps `aizk.phvv.me` to `caddy:8081`. Caddy
forwards the MCP endpoint and FastMCP OAuth routes to `server:8080`, forwards only
`PUT /api/uploads/*` to `api:8010`, refuses every other `/api` request, and sends every remaining
path to the SvelteKit server at `web:3000`. Set `AIZK_MCP_PUBLIC_URL` and
`AIZK_WEB_PUBLIC_URL` to `https://aizk.phvv.me`. This keeps both clients same-origin and avoids
exposing any backend port.

The Logto session lives in an encrypted HttpOnly cookie the SvelteKit server manages through
`@logto/sveltekit`. No token, role, permission, or organization snapshot reaches
script-readable browser storage. Every server load exchanges the session for a short-lived
bearer token whose audience is the shared MCP resource, and the API resolves the current Logto
account and organization authority on every request. Missing and suspended accounts are
rejected. The global `aizk-user` role must still be present. The request then executes through
the same transport-neutral `Memory` service used by MCP. Durable knowledge and workflow state
remain in PostgreSQL, so the browser layer does not replace AIZK's role-aware engines,
caller-bound sessions, migrations, or forced row security.

Sign-out runs the full OpenID Connect end-session flow through Logto, clearing the local cookie
and the centralized Logto session before returning home. Account settings links to Logto's
hosted Account Center, which owns password, profile, MFA, recovery, authorized application, and
global session management.

Logto also owns the hosted sign-in and registration experience. Prefer its application-level
branding, organization branding, and custom CSS over rebuilding credential forms in the app.
`Bring your UI` is the deeper customization path when CSS is insufficient. It replaces Logto's
hosted experience with a custom application backed by the Experience API, so it should be adopted
only when the standard hosted flow cannot express the desired interaction.

Organization administration also uses uncached Logto authority. An administrator adds only a
preexisting account resolved by one exact email match, then assigns one configured organization
role. AIZK sends no invitation. Role changes and removals recheck effective permissions and cannot
remove or demote the final administrator. Each successful mutation invalidates cached authority for
the actor, affected member, organization directory, and organization catalogs.

## Network boundary

PostgreSQL, Logto, the Logto admin console, MCP, the browser UI, and every model endpoint bind only
to `127.0.0.1` on the host only for Grafana operator access; every other host publication has
been removed. The Cloudflare container reaches `logto:3001` and `caddy:8081` over the Compose
network and publishes no inbound host port. Caddy reaches `server:8080`, `api:8010`, and
`web:3000` internally, with only capability upload PUTs routed publicly to the API. See the
[Docker Compose port reference](https://docs.docker.com/reference/compose-file/services/#ports).

ClamAV TCP has no protocol authentication or encryption. It is safe here only because it remains
an internal single-host service. SeaweedFS requires dedicated S3 credentials even on the internal
network. Docling requires its own API key and runs its bounded local worker engine. AIZK sends
conversion work through PgQueuer in PostgreSQL, so neither Docling nor AIZK uses Redis.

PostgreSQL TLS is off inside the single-host Compose network. This is acceptable only while the
database remains loopback-bound and every client runs on the same trusted host. A future remote
database must use certificate-verified TLS and an app URL with `sslmode=verify-full`.

## Crimson storage layout

Crimson was measured on July 15, 2026. It has three SSDs.

| Device | Capacity | Current role |
| --- | --- | --- |
| Samsung 980 PRO NVMe | 1 TB | Ubuntu, Docker, containers, and model cache |
| WD Blue SN580 NVMe | 2 TB | All durable AIZK data for the current deployment |
| Crucial MX500 SATA SSD | 2 TB | Datasets, checkpoint overflow, and future backup staging |

The second NVMe is mounted at `/mnt/ssd2`. AIZK may keep PostgreSQL, original artifacts,
observability state, OAuth state, and local backup staging on this device for now. Separate
subdirectories keep ownership and backup policies explicit even though the physical device is
shared.

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

PostgreSQL remains the latency-sensitive workload. Monitor database latency and object ingestion
together. Move SeaweedFS, observability, or backup staging to the SATA SSD if they create
measurable contention. The portable named-volume defaults remain suitable for development but do
not prove which physical disk stores the bytes.

Object storage needs its own capacity alert and external backup. PostgreSQL backups preserve blob
metadata, authorization, integrity hashes, companion text, normalized Markdown, and Docling JSON,
but they do not contain original artifact bytes. Back up the SeaweedFS data with a matching
generation of the PostgreSQL archive and test restoration of both together.

Compose accepts either a Docker volume name or an absolute host directory. Never change a
populated service path until a verified backup exists and the service is stopped. The PostgreSQL
process in the pinned image uses UID and GID `999`, so its host directory must be owned by
`999:999` with mode `0700`. Other service directories must use the UID exposed by their pinned
images rather than inheriting PostgreSQL ownership.

Crimson currently mounts PostgreSQL at `/mnt/ssd2/aizk/postgres`. The other absolute paths above
are the intended same-device layout as their services move from named volumes. Before moving an
existing populated volume, preserve separate Aizk and Logto archives, stop its service, copy the
data with ownership intact, recreate only that service, and verify its health. Finish storage
changes with the RLS lint, a restore drill, and authenticated end-to-end recall. Never remove the
old volume before all three checks pass.

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
docker compose --env-file .env -f src/deploy/docker-compose.yml exec -T db \
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

Treat these values as an initial measured operating point. Inspect `pg_stat_statements`, cache hit
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
power, controller, administrator account, and physical location. Copy every successful dump and
SeaweedFS artifact generation to an encrypted destination on another machine or object store.
Keep at least one older generation outside the normal retention window. Alert when replication
fails.

Run an on-demand Aizk backup inside the private worker.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml exec -T worker \
  aizk db backup /backups/aizk-$(date +%F).dump
```

The configured-database restore streams the archive into `pg_restore` with `--exit-on-error` and
`--single-transaction`. PostgreSQL therefore commits the complete restore or rolls it back after
the first error. This live replacement also uses `--clean --if-exists` so objects already in the
configured database do not collide with the archive.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml exec -T worker \
  aizk db restore /backups/aizk-2026-07-17.dump
```

Stop public traffic and take a fresh backup before this command. The lower-level restore path can
target an explicitly named empty scratch database. That form keeps `--exit-on-error` and
`--single-transaction` but omits `--clean --if-exists`, so a scratch drill cannot silently erase an
existing target. After either form, AIZK probes the BM25 tokenizer and recreates it when the archive
did not restore that extension state.

Database archives refer to the fixed `aizk_admin`, `aizk_app`, and `logto` roles. Initialize a new
cluster with the committed `initdb/roles.sh` before restoring either database. Do not restore
archived password hashes. The script is idempotent and reconciles every role with the current
project `.env`. Run it again after any lower-level role restore or secret rotation.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml exec -T db \
  /docker-entrypoint-initdb.d/roles.sh
```

The PostgreSQL health check authenticates over TCP with the current project secret. It therefore
detects password drift that `pg_isready` alone would miss. If the role script was replaced through
an rsync deployment, recreate only the database container before running it because a live Docker
bind mount retains the old inode. The data directory remains mounted and is not removed.

Logto is a separate database and needs a separate archive. An Aizk dump does not contain Logto
accounts, organization membership, OAuth clients, or consent records.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml exec -T db \
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

Inspect the dedicated device and both other SSDs.

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

## Logs and durable usage

The optional observability profile centralizes Docker stdout and stderr for every service in this
Compose project. AIZK processes emit Loguru JSON. PostgreSQL contributes slow statements, lock
waits, checkpoints, and ordinary server diagnostics. Logto, model services, Docling, ClamAV,
SeaweedFS, Caddy, and Cloudflared retain their native structured or plain output. Alloy discovers
containers through the read-only Docker socket and attaches the Compose service and container
labels. Loki stores 30 days on its own volume. Grafana has a provisioned Loki data source and is
the only observability port published to loopback.

Set an independent Grafana administrator password, then start the profile.

```sh
AIZK_GRAFANA_ADMIN_PASSWORD=
AIZK_DOCKER_GID="$(stat -c %g /var/run/docker.sock)"
docker compose --profile observability --env-file .env -f src/deploy/docker-compose.yml up -d
```

The one-shot `observability-init` service prepares the Loki, Alloy, and Grafana volumes for each
service's unprivileged user. This step is required even for Docker named volumes because dropping
all Linux capabilities prevents those users from repairing root-owned volume directories at
startup. `AIZK_DOCKER_GID` grants Alloy only the supplemental host group needed to open the Docker
socket while its process and state directory remain owned by UID 473.

Open `http://127.0.0.1:3003` locally or forward that loopback port over SSH. Never expose Grafana,
Loki, Alloy, or the Docker socket to the public network. Read-only Docker socket access still gives
Alloy broad visibility into container metadata and logs, so treat Alloy as host-observability
infrastructure rather than an untrusted application.

Logs explain failures but do not own billing or quotas. They expire and may be dropped. The
immutable PostgreSQL `usage_event` ledger records each successful recall, text memory, file memory,
share, and original-resource read. Each event records the authenticated actor, exact target scope
IDs, request bytes, response bytes, item count, duration, and capture time. A multi-scope event is
attributed to every target because each organization participated in that operation. Actor totals
remain the nonduplicated view.

Owner health reports expose operation totals by actor and target scope. Exact scope-set storage
reports show artifact revision count and logical original bytes. Global storage separately shows
unique physical Blobs, original bytes, stored bytes, and bytes saved by compression. This split
keeps organization attribution useful without pretending a Blob shared across two scopes occupies
disk twice. Document and graph counts remain in the per-scope corpus report.

## Five-second health overview

The bounded health command checks the migration head, forced RLS, principal table counts, queue
failures, served model aliases, model context, per-scope graph progress, operation usage, exact
scope-set file usage, physical Blob cost, recent writes, and one real recall. Database and model
probes run concurrently and recall has a 3.5 second timeout.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml exec -T worker aizk db health
```

Run this owner-level diagnostic in `worker`, never `server`. The public process intentionally has
no migration-owner credential, so a compromised MCP request path cannot turn the health command
into an RLS bypass.

A healthy public deployment reports an up-to-date migration, no RLS violations, Logto identity
mode, reachable and correctly named models, no retained queue failures, processed chunks catching
up with stored chunks, and a nonempty recall sample without an error.

## Read-only extraction diagnosis

Inspect one stored chunk through the same model and deterministic grounding rules used by graph
projection.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml run --rm --no-deps worker \
  graph diagnose-extraction 019f6bf4-ec29-72c5-93d2-59f791ae42d0
```

Run this command through `worker`, never `server`. It needs owner access to inspect an arbitrary
stored chunk but does not mark the chunk processed and does not write graph rows. The JSON output
contains the proposed extraction, the rejection reason for each fact, and the grounded subset with
acceptance counts. `missing_quote`, `unsupported_quote`, `unresolved_endpoint`, `self_relation`,
and `generic_relation` are deliberate evidence failures rather than transport errors.

## Authentication and upgrades

A public deployment uses Logto as the only identity and organization authority. FastMCP exposes
an OAuth proxy to MCP clients while Logto handles human login. The committed public startup gate
requires the public MCP URL, Logto URL, Logto Management API client, OAuth web client, and their
secrets. It first applies `src/deploy/logto.conf`, then validates the complete authentication settings.
The browser gate separately requires its public HTTPS origin, Traditional Web application, and
session secret. The session secret must contain at least 32 bytes and differ from client
secrets. The interface image bakes no deployment configuration at build time. The Logto web
secret and session secret enter only the `web` service at runtime, and the database app
credential enters only the `api` service.
Client-specific setup lives in [MCP clients](mcp-clients.md), and the complete authority model
lives in [Identity and sharing](engine/identity.md).

Every external image uses a validated version tag. VectorChord Suite currently provides only the
floating `pg18-latest` suite tag, so that image and the artifact services also carry tested
digests. Update images deliberately, inspect release notes, take both database backups and an
object-store backup, rebuild, run migrations through the one-shot setup service, and finish with
the full health probe. Never use `down -v` during an upgrade.
