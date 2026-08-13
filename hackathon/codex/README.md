# Isolated Codex Luna rehearsal

This directory holds the complete nonsecret workspace used to test crAIZK through Codex
`0.147.0` and `gpt-5.6-luna`. Run these commands from the AIZK repository root on Linux.

Build the pinned client image.

```sh
docker build \
  --build-arg CODEX_VERSION=0.147.0 \
  --tag craizk-codex-rehearsal:0.147.0 \
  hackathon/codex
```

Create disposable state and copy the committed configuration into it.

```sh
rehearsal_root="$(mktemp -d)"
mkdir -p "$rehearsal_root/home" "$rehearsal_root/workspace/.codex"
cp hackathon/codex/AGENTS.md "$rehearsal_root/workspace/AGENTS.md"
cp hackathon/codex/config.toml "$rehearsal_root/workspace/.codex/config.toml"
```

The dated rehearsal used a short-lived AIZK bearer token as `CRAIZK_MCP_TOKEN`. The token must never
be written into this directory.

The current direct-Logto configuration is in `config.oauth.toml`. It uses the pre-registered public
Native client, a fixed loopback callback port, and PKCE with no client secret. Codex `0.147.0`
discovers Logto directly from the raw Function URL metadata. `config.toml` remains the reproducible
short-lived bearer option for noninteractive rehearsals.

Run Codex with Docker as the outer security boundary. The host Codex authentication file is mounted
read only because the rehearsal used an existing ChatGPT sign-in instead of an OpenAI API key.

```sh
docker run --rm \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 256 \
  --memory 1g \
  --cpus 2 \
  -e CRAIZK_MCP_TOKEN \
  -v "$rehearsal_root/workspace:/workspace:ro" \
  -v "$rehearsal_root/home:/home/node/.codex:rw" \
  -v "$rehearsal_root/workspace/.codex/config.toml:/home/node/.codex/config.toml:ro" \
  -v "$HOME/.codex/auth.json:/home/node/.codex/auth.json:ro" \
  craizk-codex-rehearsal:0.147.0 \
  exec \
  --model gpt-5.6-luna \
  --dangerously-bypass-approvals-and-sandbox \
  --ephemeral \
  --skip-git-repo-check \
  --json \
  "Call only the aizk status tool exactly once."
```

`--dangerously-bypass-approvals-and-sandbox` applies only inside this already restricted container.
The container receives no source tree, Docker socket, AWS credentials, shell history, or writable
host workspace.

The machine-readable timings, token counts, authentication limitation, and result identifiers are
in [`../results/codex-luna-clean-room-2026-08-12.json`](../results/codex-luna-clean-room-2026-08-12.json).
