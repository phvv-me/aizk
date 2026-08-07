---
title: "Moving to a new domain"
description: "Changing the public hostnames without detaching every identity and scope in the store."
---

Most of a domain move is boring. New hostnames on the tunnel, new redirect URIs in Logto, a few
environment values. One part is not boring at all, and it fails silently, so it comes first.

## The setting that must not follow the domain

`AIZK_IDENTITY_URL` looks like configuration and behaves like a schema constant. It is the
namespace every identity is hashed into, never an address anything is fetched from.

```python
def subject_id(self, subject: str) -> UUID5:
    namespace = str(self.identity_url).rstrip("/")
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}/subjects/{subject}")


def scope_id(self, external_id: str) -> UUID5:
    namespace = str(self.identity_url).rstrip("/")
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}/scopes/{external_id}")
```

Two things come out of that namespace. Every user id, and every **scope** id. Scope ids are
stored in the `scopes` array on documents, chunks, entities, facts and profiles, and row level
security matches a caller's scopes against those arrays.

```text
  AIZK_IDENTITY_URL ──┬──▶ uuid5(ns, "<url>/subjects/<sub>") ──▶ user id
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

:::danger[Pin it before you touch anything else]
Set `AIZK_IDENTITY_URL` explicitly in the deployment's `.env`, to the value already in use, and
leave it there forever. Until it is written down it lives as a default in the source, and a
later edit that looks like tidying moves it.

```sh
AIZK_IDENTITY_URL=https://the.original.host
```

A deployment that has never stored anything may pick any namespace it likes, once.
:::

The two frozen seeds at the top of `config/settings.py`, for the anonymous and system users, are
the same thing written as literals. They stay exactly as they are, in a fork too.

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

1. **Pin `AIZK_IDENTITY_URL`** to the value already in use, and confirm the running server agrees
   before continuing.

   ```sh
   docker exec aizk-server-1 python -c "from aizk.config import settings; print(settings.identity_url)"
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
   put `AIZK_IDENTITY_URL` back rather than to touch the data.

7. **Retire the old hostnames** only once the new ones have carried real traffic. Leave the old
   redirect URIs registered for a while longer, since they cost nothing and are what a stale
   bookmark needs.

## What does not need touching

Stored `source_uri` values on artifacts fetched from the old domain stay as they are. They record
where something came from at the time, which remains true, and rewriting them would falsify
history to no benefit. Object storage keys are opaque and carry no hostname, and download URLs
are signed per request rather than stored.
