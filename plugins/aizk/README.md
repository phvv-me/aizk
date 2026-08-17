# AIZK plugin

This plugin gives Claude Code and Codex the AIZK skill and its authenticated MCP connection.

The bundled connection targets the public crAIZK demonstration. Installing the plugin never
stores credentials. The client opens Logto when the user first authenticates and keeps its own
refreshed token.

## Claude Code

```sh
claude plugin marketplace add phvv-me/aizk
claude plugin install aizk@aizk
```

Open Claude Code and use `/mcp` to sign in.

## Codex

```sh
codex plugin marketplace add phvv-me/aizk
codex plugin add aizk@aizk
codex -c mcp_oauth_callback_port=8912 mcp login aizk
```
