# Operations

Running aizk as durable, reachable infrastructure. Two things turn the engine from a local
process into something you can trust with everything and let other people into, a proven backup
and a real deployment. Both are covered here.

## The whole engine in one compose

`docker compose up -d` brings up the whole stack, the VectorChord Postgres, the three model
containers, and one `server` container that is the entire aizk engine at once. That single
container runs `serve-mcp`, which gathers the pgqueuer worker on its own event loop
(`serve_with_worker`, on by default), so one process is the MCP server, every background
maintenance pass, and the scheduled auto-backup together. There is no separate worker or backup
container to run.

```sh
docker compose up -d          # db, the model containers, and the aizk server-plus-worker
```

The `server` service reaches Postgres and the model containers by their compose service names, so
the localhost defaults that serve host-run development are overridden to the container-internal
addresses in the compose file itself, never in code. It publishes the MCP HTTP port and mounts a
`backups` volume the scheduled dumps land in.

Only when you scale horizontally, several server replicas behind a load balancer, do you split the
worker out, so the crons fire once rather than once per replica. Set `AIZK_SERVE_WITH_WORKER=0` on
the extra replicas and run one `aizk worker` beside them.

## Backup and restore

A backup is a single portable `pg_dump` custom-format archive of the whole database, the schema,
the seeded and grown ontology, every tenant's rows, the full bi-temporal history, and the
app-role grants. One file is the complete snapshot.

### The integrated auto-backup

In the compose stack the backup runs itself. It is a scheduled `BackupTask` in the same durable
pgqueuer cron that fires decay, dedup, and the rest, so the worker the server already carries dumps
the database on a cron with no separate service and no host crontab. The compose `server` service
turns it on and mounts the volume, three settings arrange it.

```sh
AIZK_BACKUP_ENABLED=1              # register the backup cron on the worker
AIZK_BACKUP_DIR=/backups           # a mounted volume so dumps outlive the container
AIZK_BACKUP_CRON="0 2 * * *"       # daily before dawn, the default
AIZK_BACKUP_KEEP_DAYS=14           # dumps older than this are pruned each run, the default
```

Each run writes a timestamped `aizk-<UTC>.dump` into the directory and prunes anything past the
keep window, so the disk never grows without bound.

### On-demand backup and restore

The same engine is two commands for a manual snapshot or a recovery.

```sh
aizk db backup /backups/aizk-$(date +%F).dump
aizk db restore /backups/aizk-2026-07-05.dump   # into the live database, destructive, --clean
```

Restoring into the live database drops each object the archive recreates so the database ends
holding exactly the backup.

### The version-match seam

`pg_dump` and `pg_restore` must be at least the server's version. The compose `server` image ships
the Postgres 18 client, matching the VectorChord server, so inside the stack the tools run right
there against `db:5432` with `pg_client_launcher` left empty. Running the CLI from a host whose own
client is older, point the launcher at the db container instead, and the archive still lands on the
host since it streams over the process's own stdio.

```sh
AIZK_PG_CLIENT_LAUNCHER=["docker","exec","-i","aizk-db-1"]
AIZK_BACKUP_DATABASE_URL=postgresql://aizk:<admin-password>@localhost:5432/aizk
```

On a managed Postgres with matching host client tools, leave both unset and the tools run directly.

### The recovery drill

A backup you have never restored is a hope, not a backup. Prove it by restoring into a fresh
scratch database and checking parity, which never touches your live data.

```sh
# create an empty scratch database, then restore into it
docker exec aizk-db-1 psql -U aizk -c "CREATE DATABASE aizk_restore_drill"
aizk db restore /backups/aizk-2026-07-05.dump  # after pointing AIZK_DB_NAME at aizk_restore_drill
```

The restore recreates the extensions and the app-role grants, and forced row level security comes
back exactly as the backup held it, so a restored copy is a working engine, not just rows. Run the
drill before you ever rely on the backups as your only durable copy.

## Deployment over HTTP with TLS

Local use runs the server over stdio. Multiple users reaching it over a network need the HTTP
transport behind TLS. The application side is one switch.

```sh
AIZK_MCP_HTTP=1
AIZK_MCP_HOST=127.0.0.1   # bind to loopback, the proxy in front is the only public listener
AIZK_MCP_PORT=8080
```

aizk does not terminate TLS itself. Put one of two things in front of the loopback-bound server.

### Option A, a reverse proxy for self-hosted TLS

A reverse proxy on the same host terminates TLS and forwards to the server. Caddy provisions and
renews a Let's Encrypt certificate on its own, so the whole TLS story is a few lines.

```caddyfile
aizk.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

### Option B, a Cloudflare Tunnel

If the domain already lives behind Cloudflare, a tunnel exposes the loopback server without
opening a port at all, Cloudflare terminates TLS at its edge and the tunnel carries traffic to
`127.0.0.1:8080`. This suits a home or office host with no public inbound.

### Authentication

A public deployment must resolve real users, which is the Zitadel path, a bearer token validated
against the issuer's JWKS and mapped to a user, provisioning one on first sight. The wiring
is in place but awaits a live end-to-end pass against a real Zitadel instance, tracked on the
[Roadmap](https://github.com/phvv-me/aizk/blob/main/ROADMAP.md). Until then the local API key and
the default user are the single-user paths, so stand a deployment up behind TLS first, then
turn on Zitadel once its flow is proven.
