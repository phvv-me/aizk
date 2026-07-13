# Operations

Running aizk as durable, reachable infrastructure. Two things turn the engine from a local
process into something you can trust with everything and let other people into, a proven backup
and a real deployment. Both are covered here.

## The whole engine in one compose

`docker compose up -d` brings up the whole stack, the VectorChord Postgres, the two model
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

## Schema upgrades

Build the replacement server image while the old server remains live. Take an Aizk database
backup, stop only the server, run `aizk migrate` from the new image, and recreate only the server.
Do not restart Logto or its preserved database for an Aizk schema upgrade.

The pre-POC schema history is one fused revision, `0001_init`. It creates the complete schema with
immutable `created_by` provenance, installs the nonempty personal, organization, and
organization-intersection scope lattice under forced RLS with every policy granted to the
`aizk_app` role rather than PUBLIC, and builds `live_fact` as a security-invoker,
security-barrier view. Before the proof of concept, upgrading from the discarded history means
backing up the Aizk database, dropping only that database, and letting `aizk db setup` create the
fresh baseline. Never stamp an older live schema as `0001_init`, and never touch Logto's separate
database or data volume during this reset.

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

The MCP server always uses streamable HTTP. Bind it to loopback and put TLS in front when it is
reachable over a network.

```sh
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

A public deployment uses Logto as the only identity and organization authority. aizk validates
the bearer signature, issuer, expiry, resource audience, and required scopes. It derives the
personal scope from the signed subject, then resolves current organizations and roles through the
Management API. It never provisions or mirrors a user, organization, membership, role, or
permission row.

The authority lookup and public organization behavior are specified in
[Identity and sharing](engine/identity.md). Management lookup failures remove shared and public
standing while preserving only a verified subject's personal scope.

Organization authority and public discovery use a Logto M2M application with a role that includes
the built-in Management API `all` permission. The token exchange uses HTTP Basic client
authentication and the OSS resource indicator `https://default.logto.app/api`.

### Logto upgrades

The crimson compose pins Logto 1.41.0. Do not deploy `latest` against the preserved Logto database.
A controlled upgrade pulls one reviewed version, backs up the Logto database, applies that image's
Logto CLI database alterations, and only then starts the service.

```sh
npm run cli db alt deploy
```

`logto db seed` is first-install initialization and is not the upgrade mechanism. Never hide a
failed seed or alteration behind `|| true`. The deployment should stop when identity schema work
fails, while the previous pinned Logto container and database remain intact.
