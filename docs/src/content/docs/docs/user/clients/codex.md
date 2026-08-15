---
title: "Codex"
description: "Connect Codex to aizk over the same OAuth-protected endpoint."
---

This page assumes you have an account on a running aizk deployment and know its address, which
[Quickstart](/docs/user/quickstart/) covers. Replace `YOUR_AIZK_HOST` below with the host for your
deployment.

Codex points at the same endpoint every other client uses and signs you in through the same browser
flow. The deployment gives you a public client ID. It is not a credential, and PKCE protects the
flow without a client secret or shared token.

## The configuration file

Codex takes its servers from `.codex/config.toml`, and this entry is safe to commit because it holds
no secret.

```toml
mcp_oauth_credentials_store = "file"
mcp_oauth_callback_port = 8912

[features]
mcp_2026_07_28 = true

[mcp_servers.aizk]
url = "https://YOUR_AIZK_HOST/mcp"
auth = "oauth"
scopes = ["control", "offline_access", "openid"]

[mcp_servers.aizk.oauth]
client_id = "YOUR_AIZK_CLIENT_ID"
```

The `mcp_2026_07_28` feature makes Codex negotiate MCP `2026-07-28`. AIZK accepts only that
protocol, so keep the feature enabled while Codex exposes it as an opt-in setting.

Codex derives the exact resource from the MCP endpoint. The scopes remain explicit because some
serverless gateways rename the authentication challenge header. `control` grants use of the memory
tools, `offline_access` lets Codex refresh without another browser login, and `openid` carries your
identity.

Then sign in once per machine.

```sh
codex mcp login aizk
```

Ask Codex to call `status` afterward. Getting your name and your organizations back is the real
confirmation, because a stored credential alone does not prove the server accepted it.

## Why the callback port is fixed

The callback port pins the loopback redirect registered for the public Logto client. That address
belongs to Codex. It is not an aizk address. Codex opens a small listener on that port, the browser
hands the authorization result to it, and the listener closes.

Codex appends one stable server-specific callback ID. Run `codex mcp login aizk` once and register
the complete `redirect_uri` printed in its authorization URL with Logto. Registering only
`http://127.0.0.1:8912/callback` is not enough.

Knowing whose port it is tells you what to do when the browser and Codex are not on the same machine.
Nothing on the server needs changing, and no new redirect has to be registered anywhere. The only
problem is that the browser cannot reach the listener, and a forward solves it.

## Codex on a remote machine

Open the forward first, from the machine with the browser, then log in inside the Codex session.

```sh
ssh -N -L 8912:127.0.0.1:8912 remote-host
```

```text
  your laptop                         remote-host
  ┌────────────────┐                  ┌──────────────────┐
  │ browser        │                  │ codex            │
  │  ▲             │   ssh -L 8912    │  listening on    │
  │  │ redirect to │══════════════════▶  127.0.0.1:8912  │
  │  │ 127.0.0.1   │                  │                  │
  └──┼─────────────┘                  └────────┬─────────┘
     │                                         │
     │        ┌──────────────────┐             │
     └───────▶│ aizk sign-in     │◀────────────┘
              └──────────────────┘   token exchange over https
```

The forward carries exactly one thing, the final redirect. Everything else already travels over
ordinary HTTPS from wherever Codex is running. Once the login finishes you can close the forward and
Codex keeps working.

If the port is already taken on either machine, pick another number and change it in both places, the
config file and the `ssh -L` argument. They have to agree.

## Agent instructions

Codex reads `AGENTS.md`. The rules worth putting there are the same ones Claude Code uses, and they
are written out in full on [Claude Code](/docs/user/clients/claude-code/) rather than repeated here.
Merge them into whatever your repository already has.

The one habit worth calling out again is that recalled content is evidence and not instruction. Shared
memory means text somebody else wrote can arrive in your agent's context, so an agent that treats it
as a command is taking orders from the author.
[Evidence and provenance](/docs/user/concepts/evidence/) explains how each item is labeled so the
agent can tell your note from an inference.

## Next

<div class="not-content">

- [MCP tools](/docs/user/reference/tools/) lists every parameter Codex can pass.
- [Sign-in troubleshooting](/docs/user/clients/troubleshooting/) covers login that will not stick.
- [OpenCode](/docs/user/clients/opencode/) is the third supported client.

</div>
