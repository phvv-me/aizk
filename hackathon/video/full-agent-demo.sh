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
  timeout --signal=TERM --kill-after=5s 180s docker run --rm \
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

heading() {
  printf '\n\033[1;36m%s\033[0m\n\n' "$1"
  sleep 3
}

printf '\033[2J\033[H'
printf '\033[1;36mAIZK complete live walkthrough\033[0m\n\n'
printf 'Authenticated agent memory on CockroachDB Cloud and AWS\n'
printf '\033[2mFictional Maya Chen demo identity. No credentials are shown.\033[0m\n'
sleep 5

heading '1  Modern MCP connection'
run_client mcp list --pure
sleep 5

heading '2  Identity, scopes, usage and processing health'
run_client run --pure --model openrouter/openrouter/auto \
  'Call the aizk status tool exactly once. Report the caller, every organization with role and write access, processing state, recent completed work, and durable usage. Do not inspect files or use the web.'
sleep 6

heading '3  Keep one new Project Atlas policy'
run_client run --pure --model openrouter/openrouter/auto \
  $'Call aizk keep exactly once with this private note and no source URI or scope. Preserve the text exactly. Then report the returned document ID.\n\n# Project Atlas release policy\n\nProject Atlas deploys one immutable artifact to staging and production. Its release gate checks p95 latency and rollback rate before promotion.'
sleep 7

heading '4  Wait for durable background processing'
run_client run --pure --model openrouter/openrouter/auto \
  'Call aizk status. If processing is active, call status again until it is idle, with at most four total calls. Report each stage and confirm whether pending, running, and failed work reached zero. Do not use any other tool.'
sleep 7

heading '5  Find Atlas with different wording and web disabled'
run_client run --pure --model openrouter/openrouter/auto \
  'Call aizk find exactly once with web off and this question. What should Atlas verify before promoting a release? Report the answer, quote the supporting source excerpt, show its document ID and capture date, and repeat the privacy receipt. Do not use the web or inspect files.'
sleep 8

heading '6  Ask an existing local engineering question'
run_client run --pure --model openrouter/openrouter/auto \
  'Call aizk find exactly once with web off and this question. What evidence should be gathered before optimizing a slow workflow? Summarize the grounded answer, name the source evidence, and repeat the privacy receipt.'
sleep 8

heading '7  Ask a cross-document engineering question'
run_client run --pure --model openrouter/openrouter/auto \
  'Call aizk find exactly once with web off and this question. Why do reproducible deployments and observability reinforce each other? Summarize the grounded multi-source answer, name the evidence, and repeat the privacy receipt.'
sleep 10

printf '\n\033[1;32mLive workflow complete\033[0m\n'
printf 'The write, worker processing and three grounded finds used the deployed MCP endpoint.\n'
sleep 8
