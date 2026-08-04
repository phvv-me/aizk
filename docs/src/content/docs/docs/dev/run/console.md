---
title: "The operator console"
description: "One gated hostname for Grafana, tracing and the operator pages, and the Cloudflare names that reach it."
---

Everything an operator touches lives behind one hostname, `admin.phvv.me`, and one Logto role.
It is a second Caddy site on port 8082 inside the same container that serves the public origin,
described on [Deployment topology](/docs/dev/run/topology/).

```d2
direction: right

operator: "operator browser" { shape: cloud }

edge: "Cloudflare" {
  access: "Access, optional"
  tunnel: "cloudflared"
}

host: "the Docker host" {
  caddy: "caddy:8082"
  gate: "oauth2-proxy:4180"
  logto
  frontend
  grafana
}

operator -> edge.access
edge.access -> edge.tunnel
edge.tunnel -> host.caddy
host.caddy -> host.gate: "forward_auth"
host.gate -> host.logto: "sign-in, roles claim"
host.caddy -> host.frontend: "/app/admin"
host.caddy -> host.grafana: "/grafana"
```

## One host, several paths

Paths rather than a name per tool, and the reason is money. Cloudflare's free Universal SSL
certificate covers the apex and one level of subdomain, so `admin.phvv.me` is covered while
`observability.admin.phvv.me` would need Advanced Certificate Manager at ten dollars a month.

| Match | Goes to |
|---|---|
| `/console`, `/console/*` | 302 to the Logto console origin |
| `/oauth2/*` | `oauth2-proxy:4180`, the sign-in, callback and sign-out endpoints |
| `/grafana/*` | `grafana:3000`, prefix intact |
| `/traces/*` | `phoenix:6006`, reserved for a later tracing phase |
| everything else | `frontend:3000`, which owns the operator pages under `/app/admin` |

The convenience redirect to Logto is `/console` and deliberately not `/auth`, because the SvelteKit
app owns `/auth/*` for its own Logto sign-in and sends every unauthenticated `/app/*` request
there. Redirecting `/auth` away from the frontend would swallow that flow and the operator pages
could never finish signing anyone in.

Grafana keeps its prefix because `GF_SERVER_SERVE_FROM_SUB_PATH` makes it serve `/grafana` itself,
so the rule is a plain `handle` rather than the `handle_path` that would strip it. It also keeps
its loopback host port on purpose, so a broken gate cannot lock an operator out of the dashboards.

## One gate, one role

Every path except `/oauth2/*` passes through a Caddy `forward_auth` call to
`oauth2-proxy:4180/oauth2/auth` first. A session carrying the operator role comes back 202 and the
request proceeds with `X-Auth-Request-User`, `-Email` and `-Groups` copied onto it. Anything else
comes back 401 and Caddy redirects to `/oauth2/sign_in` with the original URL as the return
address. That return address is spelled `https` by hand rather than taken from `{scheme}`, because
the site only ever hears plain HTTP once TLS has terminated at Cloudflare, and an `http` return
would drop the Secure session cookie and loop the sign-in. The site strips those three headers off
every inbound request before the gate runs, so a client can never arrive already claiming to be
someone.

Authorization is the `aizk-admin` role from `src/deploy/logto.conf`, read out of Logto's `roles`
claim, which the `roles` scope adds to the token. Granting console access is a role assignment in
Logto and nothing else, and `aizk admin auth roles` prints who currently holds it. The `AIZK Admin`
application in Logto carries Mandatory MFA set to passkey, so operators answer a second factor
without changing how anyone signs in to the memory itself.

## The Cloudflare hostnames

The tunnel is externally managed, so these public hostnames are added in the Cloudflare dashboard
rather than in this repository.

| Public hostname | Service |
|---|---|
| `aizk.phvv.me` | `web:8081` |
| `auth.phvv.me` | `logto:3001` |
| `admin.phvv.me` | `web:8082` |
| `console.phvv.me` | `logto:3002` |

All four are first-level subdomains, which is what keeps the certificate free.

:::caution[Move the Logto console in one step]
Logto's console must own an origin. Serving it under a path was asked for in 2022 and never
implemented, so a subpath deployment answers 404, and that is why it cannot join `admin.phvv.me`
as `/console`. It moves to `console.phvv.me` instead. Add the Cloudflare hostname and change
`AIZK_LOGTO_ADMIN_ENDPOINT` in the same step, because Logto bakes that endpoint into the console's
own redirect URIs and a half-done cutover locks the console out. Keep it to one line so the revert
is one line too.
:::

Cloudflare Access can sit in front of `admin.phvv.me` as a second, independent layer. Its free tier
covers 50 users and its Independent MFA policy asks for a WebAuthn key at the edge, before a
request ever reaches the tunnel. It is worth adding because it fails differently than the Logto
gate does, so one of them being wrong does not open the console. It stays optional, and the
oauth2-proxy gate is what the deployment actually depends on.

## Next

<div class="not-content">

- [Deployment topology](/docs/dev/run/topology/) has every service and the public routing table.
- [First start](/docs/dev/run/first-start/) walks the Logto applications and hostname cutover.
- [The security model](/docs/dev/run/security/) explains why the process split looks like this.

</div>
