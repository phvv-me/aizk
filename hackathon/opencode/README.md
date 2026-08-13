# Isolated OpenCode rehearsal

This directory is the complete nonsecret workspace used to test crAIZK as an external OpenCode
client. Run it from the AIZK repository root on Linux. The image pin is deliberate because current
OpenCode `1.18.18` does not negotiate MCP `2026-07-28`. The pinned `1.18.8` image passed direct
Logto OAuth and an authenticated `status` call on August 13, 2026.

Prepare disposable client state.

```sh
rehearsal_root="$(mktemp -d)"
mkdir -p \
  "$rehearsal_root/data" \
  "$rehearsal_root/local-state" \
  "$rehearsal_root/cache" \
  "$rehearsal_root/config"
```

Authenticate. Host networking is used only here because OpenCode listens for the configured OAuth
callback on `127.0.0.1:8912` inside its own process.

```sh
docker run --rm \
  --network host \
  --user "$(id -u):$(id -g)" \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 256 \
  --memory 1g \
  --cpus 2 \
  -e HOME=/sandbox \
  -e OPENCODE_DISABLE_DEFAULT_PLUGINS=1 \
  -v "$PWD/hackathon/opencode:/workspace:ro" \
  -v "$rehearsal_root/data:/sandbox/.local/share/opencode:rw" \
  -v "$rehearsal_root/local-state:/sandbox/.local/state:rw" \
  -v "$rehearsal_root/cache:/sandbox/.cache/opencode:rw" \
  -v "$rehearsal_root/config:/sandbox/.config/opencode:rw" \
  -w /workspace \
  ghcr.io/anomalyco/opencode@sha256:bc4de2a82a5663c9bbc2f3be7cab2a5d7dd34f7af73b59b146aa34c054bf0525 \
  mcp auth aizk --pure
```

Repeat the same command without `--network host` and replace the final arguments with `mcp list
--pure`. A successful result reports `aizk connected` through the ordinary Docker bridge.

For an agent call, add `-e OPENROUTER_API_KEY` and replace the final arguments with this command.
The key stays in the container environment and is never written into this workspace.

```sh
run --pure \
  --model openrouter/openrouter/auto \
  "Call the aizk status tool exactly once."
```

Log out before deleting the disposable directory if the rehearsal was interactive.

```sh
mcp logout aizk --pure
```

The tested workflow and timings are recorded in
[`../results/opencode-clean-room-2026-08-12.json`](../results/opencode-clean-room-2026-08-12.json).
The direct Logto retest is recorded in
[`../results/opencode-direct-logto-2026-08-13.json`](../results/opencode-direct-logto-2026-08-13.json).
