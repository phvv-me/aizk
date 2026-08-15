---
title: "Moving to a new domain"
description: "Changing the public hostnames without detaching every identity and scope in the store."
---

Most of a domain move is boring. New hostnames on the tunnel, new redirect URIs in Logto, a few
environment values. One part is not boring at all, and it fails silently, so it comes first.

## The setting that must not follow the domain

The namespace every identity is hashed into is `_IDENTITY_NAMESPACE` in `config/settings.py`.
It is a name rather than an address, in the same sense that nobody fetches
`http://www.w3.org/1999/xhtml`. A domain one controls is the ordinary way to get a globally
unique name without a registry, which is what XML namespaces and Java package names do, and
`uuid.NAMESPACE_URL` is RFC 4122's namespace for URLs so a URL shaped seed is its intended input.

It is not a setting and cannot be set from the environment, which is the point. It was a
configurable `identity_url` typed as a URL until that was understood, and a value that looks like
a deployment address is an invitation to update it when the deployment moves.

The source contains a fixed namespace string. Treat the value as an opaque identifier. It is not a
deployment example and must not be copied into a new setting or changed during a domain move.

Two things come out of that namespace. Every user id, and every **scope** id. Scope ids are
stored in the `scopes` array on documents, chunks, entities, facts and profiles, and row level
security matches a caller's scopes against those arrays.

```text
  _IDENTITY_NAMESPACE ┬──▶ uuid5(ns, "<url>/subjects/<sub>") ──▶ user id
                      └──▶ uuid5(ns, "<url>/scopes/<name>")  ──▶ scope id
                                                                    │
   stored rows carry scope ids ─────────────────────────────────────┤
   the caller arrives carrying scope ids ────────────────────────────┤
                                                                    ▼
                                              row security matches them, or hides the row

  move the namespace and the two sides stop matching:

   old ids on disk   ▓▓▓▓▓▓▓▓▓▓        rows still there, still intact
   new ids on caller           ░░░░░░  match nothing
   what recall returns          (empty, and no error anywhere)
```

So changing this value does not rename anything. It mints a second, disjoint universe of ids.
Every stored row keeps pointing at scopes that no longer exist, every caller arrives holding
scopes that match nothing, and row security correctly hides all of it. Nothing errors. Recall
returns nothing, the dashboard shows an empty account, and the data is still sitting in the
tables untouched. It is recoverable only by putting the old value back, which is why the failure
is worse than a crash would be.

:::danger[Leave the constant alone]
Do not edit `_IDENTITY_NAMESPACE` when preparing a fork or a rename. Its value is arbitrary and its
stability is not, exactly like a UUID namespace, which is what it is. The anonymous and system ids
derive from the same constant, so the rule covers all three at once.

A deployment that has never stored anything may choose a different value once, before its first
write, and never again.
:::

## What actually has to change

Everything below is safe to move, because nothing derives an identifier from it.

| Setting | What it is |
|---|---|
| `AIZK_MCP_PUBLIC_URL` | the MCP origin, and the `aud` every token carries |
| `AIZK_LOGTO_URL` | the issuer browsers and clients are sent to |
| `AIZK_LOGTO_ADMIN_ENDPOINT` | the origin Logto's console owns, which cannot be a path |
| `AIZK_ADMIN_PUBLIC_URL`, `AIZK_ADMIN_HOST` | the operator console and its cookie domain |

Plus the tunnel hostnames, and every redirect URI registered on a Logto application.

## Order of operations

Keep Logto's database. Do not re-mint the tenant on the new domain. aizk derives a user id from
the token subject, and those subjects belong to Logto's records, so a fresh Logto issues new
subjects and detaches every user exactly as a changed namespace would. Moving the domain is an
endpoint change against the same database.

1. **Confirm the namespace is untouched** in whatever build is about to deploy. Nothing in the
   environment can move it, so this is a check against the source rather than the config.

   ```sh
   docker exec aizk-server-1 python -c \
     "from aizk.config.settings import _IDENTITY_NAMESPACE; print(_IDENTITY_NAMESPACE)"
   ```

2. **Add the new hostnames to the tunnel** while the old ones still resolve. Nothing points at
   them yet, so this costs nothing and can be undone by deleting them.

3. **Add the new redirect URIs to every Logto application, beside the old ones.** Both sets valid
   at once is what makes the cutover reversible. The browser app, the operator gate and the MCP
   OAuth proxy each own theirs, and a client with only the old URI registered fails closed at
   sign in with `invalid_redirect_uri`.

4. **Change the endpoints in one step**, `AIZK_LOGTO_URL`, `AIZK_LOGTO_ADMIN_ENDPOINT`,
   `AIZK_MCP_PUBLIC_URL`, `AIZK_ADMIN_PUBLIC_URL` and `AIZK_ADMIN_HOST` together, then recreate
   Logto and the runtime services. A half done cutover locks the console out, because Logto bakes
   its admin endpoint into the console's own redirect URIs.

5. **Re-authenticate MCP clients.** `mcp_resource_id` is the public URL plus `/mcp` and it is the
   `aud` claim, so every existing token is now for the wrong audience and is refused. This is
   expected, it is not a fault to debug, and it is the one unavoidable interruption for users.

6. **Verify before removing anything.** Sign in through the browser app, open the operator
   console, and complete one MCP tool call. Then check that recall still returns rows, which is
   the assertion that the namespace survived.

   ```sh
   docker exec aizk-worker-1 aizk admin health
   ```

   `row_counts` unchanged and a `recall` block with candidates is the proof. Zero candidates
   against a populated `row_counts` is the namespace failure described above, and the fix is to
   put the constant back rather than to touch the data.

7. **Retire the old hostnames** only once the new ones have carried real traffic. Leave the old
   redirect URIs registered for a while longer, since they cost nothing and are what a stale
   bookmark needs.

## What does not need touching

Stored `source_uri` values on artifacts fetched from the old domain stay as they are. They record
where something came from at the time, which remains true, and rewriting them would falsify
history to no benefit. Object storage keys are opaque and carry no hostname, and download URLs
are signed per request rather than stored.
