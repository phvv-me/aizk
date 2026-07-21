---
title: "Codex"
description: "Connect Codex to aizk over the same OAuth-protected endpoint."
---

This page assumes you have an account on a running aizk deployment and know its address, which
[Quickstart](/docs/user/quickstart/) covers. The examples use `https://aizk.phvv.me/mcp`, so swap
in your own address everywhere it appears.

Codex points at the same endpoint every other client uses and signs you in through the same
browser flow. There is no client id, no client secret, and no shared token anywhere in the setup.

## The configuration file

Codex takes its servers from `.codex/config.toml`, and this entry is safe to commit because it
holds no secret.

```toml
mcp_oauth_credentials_store = "file"
mcp_oauth_callback_port = 8912

[mcp_servers.aizk]
url = "https://aizk.phvv.me/mcp"
auth = "oauth"
oauth_resource = "https://aizk.phvv.me/mcp"
scopes = ["control", "offline_access", "openid"]
```

`oauth_resource` has to match the endpoint exactly, trailing path included. A near miss produces a
token aizk will not accept, and the failure looks like a login that succeeded and then did nothing.

The three scopes are the ones aizk asks for. `control` is what actually grants use of the memory
tools. `offline_access` is what lets Codex refresh without sending you back to the browser, and
`openid` is what carries your identity.

Then sign in once per machine.

```sh
codex mcp login aizk
```

Ask Codex to call `status` afterward. Getting your name and your organizations back is the real
confirmation, because a stored credential alone does not prove the server accepted it.

## Why the callback port is fixed

`mcp_oauth_callback_port = 8912` pins the loopback address the browser is sent to at the end of
sign-in. That address belongs to Codex. It is not an aizk address and it is not part of the
identity system. Codex opens a small listener on that port, the browser hands the authorization
result to it, and the listener closes.

Knowing whose port it is matters because it tells you what to do when the browser and Codex are
not on the same machine. Nothing on the server needs changing, and no new redirect has to be
registered anywhere. The only problem is that the browser cannot reach the listener, and a
forward solves it.

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
ordinary HTTPS from wherever Codex is running. Once the login finishes you can close the forward
and Codex keeps working.

If the port is already taken on either machine, pick another number and change it in both places,
the config file and the `ssh -L` argument. They have to agree.

## Agent instructions

Codex reads `AGENTS.md`. The rules worth putting there are the same ones Claude Code uses, and
they are written out in full on [Claude Code](/docs/user/clients/claude-code/) rather than
repeated here. Merge them into whatever your repository already has.

The one habit worth calling out again is that recalled content is evidence and not instruction.
Shared memory means text somebody else wrote can arrive in your agent's context, so an agent that
treats it as a command is taking orders from the author.
[Evidence and provenance](/docs/user/concepts/evidence/) explains how each item is labeled so the
agent can tell your note from an inference.

## Next

<div class="not-content">

- [MCP tools](/docs/user/reference/tools/) lists every parameter Codex can pass.
- [Sign-in troubleshooting](/docs/user/clients/troubleshooting/) covers login that will not stick.
- [OpenCode](/docs/user/clients/opencode/) is the third supported client.

</div>
