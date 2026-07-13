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
claims to `LogtoClient`. The client calls `GET /api/users/{userId}/organizations`, whose official
response includes every current organization and that member's complete `organizationRoles`
array. A short coalesced TTL cache bounds request volume and membership staleness. A failed lookup
returns no shared authority. The personal scope remains valid because it comes from the verified
subject, not from the failed organization lookup.

Only the trusted Logto endpoint is configured. `LogtoClient` obtains the issuer, JWKS URI, token
endpoint, and accepted signing algorithms from its validated discovery document. The public MCP
base URL remains configured because it is the server's own trust boundary and cannot safely be
learned from an unverified token. Aizk derives its audience by adding `/mcp` to that URL.

The writable role names remain a small Aizk setting today. `editor` and `admin` grant write
standing while every returned membership grants read standing. Direct organization permissions
can replace that mapping later without changing the scope lattice or adding identity tables.

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
frozen sets validated by Pydantic. `User.bind()` carries the caller across a workflow,
`User.current()` reads it, and each database `Session.user` exposes the caller bound to that
transaction. Background work uses `User.system(scopes)` over an explicit scope set. Owner-role
maintenance stays in `bypass_rls()` because database administration is a connection privilege,
not a more powerful user token.

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

The client lists organizations through the Management API and retains only entries whose
`customData.public` value is exactly true. It uses an M2M application with the Management API
`all` permission and HTTP Basic client authentication. Failure closes access by yielding no public
organizations. FastMCP's remote provider still requires a valid bearer, so public means visible to
every authenticated caller rather than an unauthenticated internet endpoint. The anonymous User
exists for local auth-off operation and policy tests.

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
- [Management API](https://docs.logto.io/integrate-logto/interact-with-management-api)
- [Organization webhook events](https://docs.logto.io/developers/webhooks/webhooks-events)
