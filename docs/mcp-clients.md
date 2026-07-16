# MCP clients

Every client points at the same URL and discovers Aizk as an OAuth protected resource. FastMCP
registers each client dynamically, proxies human sign-in through Logto, and stores encrypted
registration and refresh state on the server.

## Codex

Commit `.codex/config.toml` in the project.

```toml
mcp_oauth_credentials_store = "file"
mcp_oauth_callback_port = 8912

[mcp_servers.aizk]
url = "https://aizk.phvv.me/mcp"
auth = "oauth"
oauth_resource = "https://aizk.phvv.me/mcp"
scopes = ["control", "offline_access", "openid"]
```

Then sign in.

```sh
codex mcp login aizk
```

The fixed port makes remote development predictable. When Codex runs on another machine, open the
forward before login.

```sh
ssh -N -L 8912:127.0.0.1:8912 remote-host
```

The final loopback callback belongs to Codex, not Aizk or Logto. The SSH forward carries that one
browser callback to the machine where Codex is listening.

## Claude Code

Commit the Aizk entry in `.mcp.json`.

```json
{
  "mcpServers": {
    "aizk": {
      "type": "http",
      "url": "https://aizk.phvv.me/mcp"
    }
  }
}
```

Claude Code starts the browser login when it first reaches the protected server. It needs no shared
OAuth credential.

## OpenCode

Commit the remote server in `opencode.json`.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "aizk": {
      "type": "remote",
      "url": "https://aizk.phvv.me/mcp",
      "enabled": true
    }
  }
}
```

Start and inspect authentication with

```sh
opencode mcp auth aizk
opencode mcp debug aizk
```

## Why login survives restarts

The Compose deployment mounts FastMCP state at `/oauth`. The public server issues a long-lived
reference token while the encrypted upstream Logto session remains authoritative and refreshable.
This works around clients that retain an expired transport token without weakening Logto
revocation.

Rotating the Aizk OAuth client secret changes the derived encryption and signing keys. That is an
intentional session reset and every client must sign in again.

## Troubleshooting

If a client reports that Aizk is not logged in, first run its login command again and confirm its
project configuration is the one being loaded. If login succeeds but startup fails after a server
restart, check that the `oauth` volume is mounted and persistent. If the browser cannot reach a
loopback callback, the browser and client are on different machines and the client port needs a
forward.

The server-side Logto application has one exact callback at
`https://aizk.phvv.me/auth/callback`. Client loopback callbacks are dynamically registered with
Aizk and never added to Logto.
