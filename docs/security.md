# Security

This page defines what a production Aizk deployment protects, what it deliberately trusts, and
which checks must pass before public traffic is allowed. It assumes the single-host Compose
deployment described in [Operations](operations.md).

## Protected assets

Aizk stores private notes, shared team sources, embeddings, graph projections, temporal history,
OAuth sessions, organization standing, and database backups. The main security goals are these.

- One caller must never read or alter another caller's private memory.
- A caller must stand in every scope attached to a shared row.
- Public request handling must not possess a credential that bypasses row security.
- OAuth tokens, client secrets, database passwords, and backups must not appear in logs or process
  arguments.
- Stored text must remain untrusted data when an agent consumes recall.
- A stolen disk or copied backup must not become the only path to the lab's memory.

## Trust boundaries

Logto owns users, organizations, memberships, roles, permissions, login, and consent. Aizk owns
the deterministic mapping from verified Logto identifiers to PostgreSQL UUID5 values. It does not
mirror identity rows.

The MCP server verifies the token signature, issuer, expiration, required `control` scope, and
the exact Aizk resource audience. It resolves organization standing through a short coalesced
Management API cache. A failed refresh closes shared authority. Cached standing can remain valid
only for the configured bounded TTL.

The browser does not use that authority cache for access decisions. Both the authorization
callback and every protected browser event load the account and global roles again. A missing
account, suspended account, or missing global `aizk-user` role is unauthorized. Organization
administration also loads memberships, effective permissions, role templates, and member
directories without the MCP cache. This makes account and collaboration changes effective on the
next browser decision.

PostgreSQL is the final data authorization boundary. The `aizk_app` role is neither a superuser
nor a `BYPASSRLS` role. Every tenant table forces row security. A source row is readable only when
all stored scopes are present in the caller's read standing. A write requires all stored scopes
in write standing. PostgreSQL table owners normally bypass RLS, which is why the public `server`
does not receive the owner password. See the [PostgreSQL row security
reference](https://www.postgresql.org/docs/18/ddl-rowsecurity.html).

The `chunk` table inherits read visibility from its parent document. Its insert and update policy
also requires the chunk and a visible parent to carry the same scope set. This closes the foreign
key loophole where a caller who guessed another tenant's document ID could otherwise attach a
child row to it. PostgreSQL referential integrity checks intentionally bypass row security, so the
policy must enforce the parent relationship itself.

## Process privilege separation

The Compose stack uses one image with separate services.

- `setup` holds owner credentials only while applying migrations and installing the queue schema.
- `server` holds the app password and OAuth credentials. Owner settings are explicitly blank.
- `worker` holds the app and owner credentials for projections, roster discovery, and backups. It
  has no published port.
- `api` holds the forced-RLS app password and answers browser JSON requests with the same Logto
  bearer verification as MCP. It receives no owner or backup credential.
- `web` is the SvelteKit server holding only the confidential Logto web application and the
  session secret. It has no database credential.
- `caddy` routes the one public origin to the server, the API, and the interface. It has no
  secret at all.
- `logto` connects as a dedicated role that owns only the separate Logto database.
- model services have no database credentials and bind only to loopback on the host.

The long-lived Aizk runtime containers use UID `10001`, a read-only root filesystem, no Linux
capabilities, `no-new-privileges`, a bounded process count, and an isolated temporary filesystem.
The one-shot volume initializer keeps only `CAP_CHOWN`, `CAP_DAC_READ_SEARCH`, and `CAP_FOWNER`,
creates mode `0700` OAuth and backup directories for UID `10001`, and exits before either
long-lived process starts.

This split reduces the effect of a public-server compromise. It does not protect against host
root compromise, Docker daemon compromise, a malicious image, or arbitrary code executed inside
the private worker.

## Network exposure

Every published host port has an explicit `127.0.0.1` binding. The Cloudflare Tunnel is outbound
and reaches the server through the Compose network. The public profile waits for Logto and the
tunnel, applies the committed Logto authorization policy, then runs `check-public` with
`AIZK_REQUIRE_AUTH=1`. A missing Logto URL, public URL, Management API client, OAuth web client, or
client secret therefore stops the MCP server.

The browser origin has a separate startup gate. The public URL must use HTTPS and one complete
Logto Traditional Web application must exist with its exact callback URL.
`AIZK_WEB_SESSION_SECRET` must contain at least 32 bytes and must differ from the web,
Management API, and OAuth client secrets. Authorization uses the code flow with PKCE, state,
and nonce.

The Logto session lives only in an encrypted HttpOnly cookie managed by the SvelteKit server.
Access tokens and refresh tokens never reach script-readable browser storage. Every server load
exchanges the session for a short-lived API bearer token, and the API resolves fresh Logto
authority before it opens a forced-RLS application session.

Public organizations affect database read standing only. They never enter writable standing. A
write requires current Logto membership and the effective `write:memory` organization permission,
then forced row security applies the same check inside PostgreSQL. The production OAuth proxy also
requires a bearer before a tool runs, so public organization content is not anonymously reachable.

Self-registration is controlled by Logto. Before opening it, require verified accounts, keep new
users out of every organization by default, and add abuse controls sized for model-backed recall.
AIZK organization administration adds only one existing account selected by exact email. It does
not send invitations or expose a browsable tenant directory. The final administrator cannot be
demoted or removed. Unauthenticated semantic recall should remain a separate read-only service if
it is ever introduced.

Cloudflare should also enforce request body size and rate limits for `/authorize`, `/register`,
`/token`, and `/mcp`. Application middleware covers MCP tool calls after FastMCP has established a
request context. It does not protect OAuth discovery, dynamic registration, authorization, or
token exchange routes from connection floods.

## Work limits

Each resolved user receives an independent five-second token bucket. The default sustained rate
is five MCP calls per second. A bounded cache holds at most 4096 caller buckets per process.

Tool schemas reject work above these defaults before it reaches PostgreSQL or a model.

| Input | Default limit |
| --- | --- |
| Recall query | 16,384 characters |
| Recall evidence budget | 16,384 tokens |
| Remembered source | 5,000,000 characters |
| Source URI | 4096 characters |
| Scope names | 32 per call |
| Shared documents | 100 per call |
| Original file | 10 MiB |

The remembered-source limit intentionally permits a large PDF-to-Markdown paper. Cloudflare and
the reverse proxy should set a request body limit slightly above the encoded MCP request rather
than relying only on application validation.

## Artifact intake and retrieval

File intake treats every byte as hostile. Browser uploads are bounded before persistence and never
enter a public upload directory or named temporary path. URI intake permits only uncredentialed
HTTPS sources whose DNS answers are all public. Every redirect repeats that validation. Response
time, declared size, streamed size, and redirect count are bounded.

Application checks reduce ordinary server-side request forgery risk. Production must also restrict
container egress so DNS rebinding cannot reach loopback, link-local, private, metadata, or internal
service networks. Docling, ClamAV, SeaweedFS, and PostgreSQL publish no public host ports.

ClamAV scans before object persistence and fails closed. Malware, an unavailable daemon, a malformed
response, or a byte-limit violation rejects intake. The scanner speaks the official NUL-framed
`INSTREAM` protocol over the private Compose network. Its TCP port must never be exposed publicly.

Stored objects use opaque random keys and service credentials with no public bucket access. The
worker verifies the stored UUIDv8 content fingerprint and original size before conversion. The
object-store client also requests a SHA-256 transport checksum while uploading. Docling receives
the accepted stored bytes rather than fetching the source again. Conversion jobs contain only an
immutable content ID and exact scopes.

Recall remains text first and includes only a compact resource identifier when evidence has an
original artifact. Reading bytes is a separate explicit request. That request resolves the current
caller, applies forced PostgreSQL row security, requires the exact original revision, and verifies
the stored digest. A guessed object key or artifact UUID grants no authority.

The initial browser path materializes one bounded upload in memory. The default limit is 10 MiB.
Larger media should use a future chunked direct-upload protocol with an equally strict scan gate.
The current service must not raise the limit casually because every concurrent upload consumes
application memory.

Only the accepted original enters object storage. Adaptive Zstandard encoding is used only when it
reduces stored bytes by the configured minimum. PostgreSQL retains the original size, stored size,
encoding, UUIDv8 content fingerprint, Markdown, Docling JSON, companion text, and conversion
metadata. Decoding and integrity verification happen before conversion or download.

Accepted images also receive one direct visual embedding after Docling has produced the
authoritative text and structure. The supplemental chunk stays on the same document and exact
artifact revision. Its provenance identifies the image modality, media type, and representation.
It never records a provider name. Video currently contributes Docling's audio transcript and
metadata only. Frame-level video semantics remain disabled until a measured implementation earns
the added cost.

An unsupported Docling input is not discarded and does not become trusted extracted text. AIZK
creates a metadata-backed document so filename, media type, size, URI, failure state, and optional
companion context remain recallable. The original resource is still subject to current RLS and an
explicit read. Operators should keep dangerous executable formats outside AIZK even when ClamAV
reports them clean.

## Stored text and prompt injection

An authorized source can still be malicious. It may contain instructions that try to override the
agent consuming recall. Aizk treats source text as data during extraction and returns recall as one
evidence string. The client skill establishes the trust boundary once rather than spending tokens
on a repeated warning in every response. That boundary reduces accidental instruction following
but cannot guarantee model behavior.

Client agents must never treat recalled text as higher priority than system or user instructions.
They must not execute commands, reveal secrets, call tools, or change authorization because a
memory source asks them to. Provenance and source authority help a client judge evidence, but they
do not turn arbitrary text into trusted instructions.

Model extraction is also a projection, not authority. Typed output and ontology validation bound
its shape. The original source remains available and higher-level graph artifacts can be rebuilt.
An authorized writer can still poison their own or a shared scope with false source material.
Write authority therefore remains a meaningful privilege.

## Secrets and OAuth state

The database owner, application, and Logto roles use independent passwords. The browser session
secret is also independent and contains at least 32 bytes. The public server explicitly removes
owner values inherited from the private environment. FastMCP stores dynamic client registrations
and upstream Logto tokens in the persistent `/oauth` volume. The current FastMCP version encrypts
that state with keys derived from the OAuth client secret.

Rotating the OAuth client secret invalidates the derived storage keys and requires every MCP
client to sign in again. Rotate a database password by updating the role and deployment secret in
one maintenance window, then recreate only the services that use that role. Never place tokens or
passwords in a repository, image layer, command argument, or support log.

Aizk reference tokens may live longer than Logto access tokens. They do not create an independent
session. Each request resolves the encrypted upstream token and refreshes or rejects it according
to Logto state. Logto revocation and membership changes remain authoritative.

## Database and backup confidentiality

PostgreSQL 18 page checksums are enabled and verified separately from encryption. Checksums detect
some corruption but reveal nothing about confidentiality and do not replace a restore test.

Core PostgreSQL has no transparent cluster encryption. Crimson's dedicated database NVMe is not
yet LUKS-encrypted and has no TPM-assisted unlock path. This is a recorded physical-security gap,
not an implicit guarantee. The exact choices are explained in [Operations](operations.md).

Local `pg_dump` files are plaintext database archives. Aizk creates them with mode `0600` and
passes the password through `PGPASSWORD` rather than the process command line. Every successful
archive must be copied to an encrypted off-host destination. The local SSD copy alone does not
survive theft, fire, controller failure, administrator error, or ransomware.

## Software supply chain

The server image installs from the committed `uv.lock` with frozen resolution. Direct GLiNER
sidecar dependencies use the exact versions validated on Crimson. External images use validated
release tags. VectorChord Suite currently exposes a floating PostgreSQL 18 suite tag, so Compose
also pins its tested digest.

vLLM runs model repositories with `trust-remote-code`. Model checkpoint selection is therefore
equivalent to selecting executable code. Only trusted model repositories and pinned revisions
belong in production. The Hugging Face cache must not be writable by untrusted users.

Dependency and image updates are deliberate changes. Inspect upstream release notes, rebuild from
scratch, run the full test and type gates, scan the resulting images, deploy privately, run the
five-second health check, and only then restore public traffic.

## Release gate

A public deployment is not ready until every item below is true.

- All host port bindings resolve to `127.0.0.1`.
- The public server environment contains no owner URL or password.
- Logto uses its dedicated role and database.
- The public authentication preflight succeeds.
- The browser authentication preflight succeeds when the UI is enabled.
- The browser public and API URLs are the same HTTPS origin.
- The browser session secret has at least 32 bytes and differs from every client secret.
- A suspended user and a user without `aizk-user` both fail the browser access check.
- Organization management adds an existing exact-email account and preserves at least one admin.
- The static frontend image contains no database password, Logto secret, or browser session secret.
- Database passwords are unique, random, and absent from tracked files.
- Alembic is at head and the RLS verifier and `pgrls lint` report no violations.
- The cross-tenant child-write regression test passes.
- MCP request limits and per-caller rate limiting are active.
- Database checksums report `on`.
- The dedicated PostgreSQL NVMe has at least 20 percent free space.
- SMART monitoring, temperature alerts, and periodic TRIM are active.
- A current Aizk archive and a current Logto archive exist off-host in encrypted storage.
- A scratch restore has passed within the last month.
- The health report finishes within five seconds and its real recall succeeds.
- Cloudflare rate and body-size rules protect both OAuth and MCP routes.
- The remaining lack of LUKS on Crimson is explicitly accepted or fixed before sensitive data is
  stored.
