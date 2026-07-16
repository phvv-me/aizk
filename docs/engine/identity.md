# Identity and sharing

This page records the implemented identity boundary. `0001_init` contains no user, organization,
membership, role, or owner authorization table. The Logto boundary and multi-organization scope
lattice are durable product rules.

## Logto owns identity

Logto is the only source of truth for users, organizations, memberships, roles, and permissions.
aizk must not add mirror tables for any of them. A verified Logto access token establishes the
subject while the Logto Management API returns that subject's current organizations and roles.
Stable UUID5 values derived from the signed `sub` and Logto organization IDs let Postgres use UUID
columns without a lookup table. `settings.identity_url` owns the namespace base while
`settings.subject_id()` and `settings.scope_id()` make the Aizk-owned mapping explicit.

The resource token must pass signature, issuer, expiration, audience, and required-scope checks.
It does not need a custom multi-organization claim. Membership and role changes take effect after
the short authority cache expires rather than waiting for a new access token.

## One subject resolves multi-organization standing

A standard Logto organization access token represents one organization. It carries one
`organization_id` and the effective scopes for that organization. That shape cannot prove that a
caller belongs to both organization A and organization B in one MCP bearer token.

Instead of changing the token, MCP `Auth` validates the ordinary Aizk resource token and passes its
claims to `LogtoClient`. The client reads the user profile and global roles, then calls
`GET /api/users/{userId}/organizations` for current memberships. For each organization it reads
the complete member directory and calls `GET /api/organizations/{id}/users/{userId}/scopes` for
effective permissions. A short coalesced TTL cache bounds request volume and membership staleness.
A failed permission lookup closes write access for that organization. The personal scope remains
valid because it comes from the verified subject, not from the failed organization lookup.

Only the trusted Logto endpoint is configured. `LogtoClient` obtains the issuer, JWKS URI, token
endpoint, and accepted signing algorithms from its validated discovery document. The public MCP
base URL remains configured because it is the server's own trust boundary and cannot safely be
learned from an unverified token. Aizk derives its audience by adding `/mcp` to that URL.

Role names have no authorization meaning inside AIZK. Every returned membership grants read
standing. Logto's effective organization permissions determine write standing through the one
deployment-configured write permission. The MCP `status` tool returns the same resolved `User`.
Its Pydantic organization models preserve Logto names, descriptions, custom data, members, roles,
and effective permissions. Cached properties index writable and public organizations without
creating identity tables. Status omits emails, phone numbers, linked identities, and internal IDs
because they do not help an agent choose a memory scope.

Global roles and organization roles are separate authorization layers. The global `aizk-user` role
grants the AIZK API permission needed to enter the service. It never grants a write into every
organization. An organization role such as editor becomes writable only when its organization
template assigns `write:memory`. Assigning the global API resource permission named `control` does
not satisfy this check. The distinct names keep service access separate from collaboration writes.

`deploy/logto.conf` declares the AIZK-owned API resource, API permissions, default global user role,
organization roles, and the organization write permission. Every field is an `AIZK_` setting that
`.env` can override. `aizk logto audit` reports drift and exits nonzero when repair is needed.
`aizk logto apply` reconciles the policy through the Logto Management API. It deletes only obsolete
global user roles under the configured managed prefix and preserves unrelated roles and
permissions.

## Scope sets are the collaboration model

The `scopes uuid[]` column is intentional. It is not a denormalized substitute for one
organization foreign key.

- A personal scope derived from `sub` holds private memory.
- One organization scope holds ordinary team memory.
- The set containing A and B holds the bridge visible only to members of both.

Every target row has a nonempty, sorted, duplicate-free scope set. The caller standing contains
their personal scope and every organization scope currently returned by Logto. A row is readable
when its whole scope set is contained by the caller's readable standing. Retrieval never accepts a
second scope selector. A user in A and B automatically reads personal, A, B, and A-and-B rows.
Writes choose one destination separately and require its complete scope set to be contained by the
caller's writable standing.

The application represents this once as `User.scopes`. Its `read`, `write`, and `public` fields are
frozen sets validated by Pydantic. `async with user` opens one short app-role transaction, applies
the user's RLS settings transaction-locally, and exposes the caller as `Session.user`. `user.app`
provides the same explicit transaction object. `user.session()` exists only for workflows that
need several explicit transactions or savepoints on one session. `user.exec[Model]` runs one typed
statement and validates its rows into the selected Pydantic model.

Background work uses `User.system(scopes)` over an explicit scope set. Only that system identity
may open `user.owner`, which connects through the database owner for migrations, backups, or the
scope roster. Owner authority is a connection privilege and never a stronger bearer token. The
public MCP process receives no usable owner URL or password, so request handling cannot choose
this path even after an application defect.

`User.write_scope()` defaults to the personal singleton. MCP writes may pass Logto organization
names to select one organization or an explicit intersection. The method resolves those trusted
names to stable scope IDs and refuses any destination outside `User.scopes.write`.

This removes `owner_id` from authorization without removing private memory. `created_by` remains
as immutable provenance derived from the signed subject, never as an access-control shortcut.

## Public organizations stay singleton public

An organization may set `customData.public` in Logto. Public status makes only that
organization's singleton scope world-readable. It must not satisfy one member of a compound
scope. A row in A and B remains restricted to authenticated members of both even when A itself is
public.

Public status never grants write standing. A caller may write into a public organization only when
they are a member and their effective Logto organization permissions include the configured
`write:memory` permission. A public reader who is not a member has the organization ID only in
`User.scopes.public`. A writer has it independently in `User.scopes.write`. `User.write_scope()`
checks the write set before opening a transaction, and forced PostgreSQL row security repeats that
check for insert and update.

The client lists organizations through the Management API and retains only entries whose
`customData.public` value is exactly true. It uses an M2M application with the Management API
`all` permission and HTTP Basic client authentication. Failure closes access by yielding no public
organizations. FastMCP's OAuth proxy still requires a valid bearer, so public means visible to
every authenticated caller rather than an unauthenticated internet endpoint. The anonymous User
exists for local auth-off operation and policy tests.

## Signup and unauthenticated access

The current deployment is invitation-only. An administrator creates the Logto account and sends
the initial credentials privately. There is no self-registration page in the AIZK flow. Once a user
authenticates, the default global `aizk-user` role grants entry to the MCP API. It does not grant
membership or write standing in any organization.

A future self-service signup should remain an authenticated flow through Logto. A new account would
receive `aizk-user`, its private memory scope, and read access to singleton public organizations. It
would receive no organization membership and no shared write access. An invitation or explicit
administrator action would still grant private organization membership and roles. Email
verification, request throttling, and abuse controls should be enabled before opening registration
because recall invokes database and model work.

True unauthenticated reading is a different feature and is not enabled. The safest public interface
for general documentation remains the static website. If unauthenticated semantic recall is added,
it should use a separate read-only surface that exposes only `recall`, binds the anonymous sentinel
with no personal or writable scopes, reads singleton public organizations only, and carries strict
rate and budget limits. It must not expose `status`, `remember`, or `share`.

## Background work follows the same scope set

The unit of maintenance is a canonical scope set rather than a row creator. Queue payloads,
watermarks, profiles, communities, RAPTOR reports, and insights must all use that scope set as
their partition key.

An A-and-B job binds read authority for A and B, which composes A, B, and bridge knowledge through
the same RLS containment rule. Derived artifacts from that pass are written into the exact A-and-B
scope set. A queued job must carry its authorized scope set. A user ID alone is insufficient
because it loses the organization standing needed for shared writes. Unique constraints,
deduplication, watermarks, profiles, queue payloads, and background rosters therefore all use the
same canonical scope key.

## Logto references

- [Python integration](https://docs.logto.io/quick-starts/python)
- [Get organizations for a user](https://openapi.logto.io/dev/operation/operation-listuserorganizations)
- [Get user](https://openapi.logto.io/operation/operation-getuser)
- [Get roles for user](https://openapi.logto.io/operation/operation-listuserroles)
- [Get organization members](https://openapi.logto.io/operation/operation-listorganizationusers)
- [Get effective organization permissions for a user](https://openapi.logto.io/operation/operation-listorganizationuserscopes)
- [Management API](https://docs.logto.io/integrate-logto/interact-with-management-api)
- [Organization webhook events](https://docs.logto.io/developers/webhooks/webhooks-events)
