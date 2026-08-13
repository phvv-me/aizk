#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
environment_file="${AIZK_DEMO_ENV_FILE:-$repository_root/.env}"
state_root="${CRAIZK_OPENCODE_STATE_ROOT:-$(< /tmp/craizk-opencode-state-root)}"
image='ghcr.io/anomalyco/opencode@sha256:bc4de2a82a5663c9bbc2f3be7cab2a5d7dd34f7af73b59b146aa34c054bf0525'

set -a
source "$environment_file"
set +a
export OPENROUTER_API_KEY="$AIZK_DEMO_OPENROUTER_API_KEY"

run_client() {
  timeout --signal=TERM --kill-after=5s 240s docker run --rm \
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
    -e OPENROUTER_API_KEY \
    -v "$repository_root/hackathon/opencode:/workspace:ro" \
    -v "$state_root/data:/sandbox/.local/share/opencode:rw" \
    -v "$state_root/state:/sandbox/.local/state:rw" \
    -v "$state_root/cache:/sandbox/.cache/opencode:rw" \
    -v "$state_root/config:/sandbox/.config/opencode:rw" \
    -w /workspace \
    "$image" "$@"
}

printf '\033[2J\033[H'
printf '\033[1;36mMore grounded questions from the same new source\033[0m\n\n'
printf 'The wording changes. The stored evidence and access boundary do not.\n'
sleep 6

printf '\n\033[1;36mExample 2  Deployment identity\033[0m\n\n'
run_client run --pure --model openrouter/openrouter/auto \
  'Call aizk find exactly once with web off. Where does Project Atlas deploy its artifact, and does it build one artifact or separate ones? Answer only from memory, name the source and document ID, then repeat the privacy receipt.'
sleep 10

printf '\n\033[1;36mExample 3  Promotion signals\033[0m\n\n'
run_client run --pure --model openrouter/openrouter/auto \
  'Call aizk find exactly once with web off. Which exact operational signals decide whether Atlas can be promoted? Answer only from memory, name the source and document ID, then repeat the privacy receipt.'
sleep 12

printf '\n\033[1;32mThree differently worded questions grounded in one durable source\033[0m\n'
sleep 10
