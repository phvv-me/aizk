#!/usr/bin/env bash
set -euo pipefail

rehearsal_root=$(mktemp -d)

finish() {
  rm -rf "$rehearsal_root"
}
trap finish EXIT

mkdir -p "$rehearsal_root/home" "$rehearsal_root/workspace"
cp "$HOME/.codex/auth.json" "$rehearsal_root/home/auth.json"
chmod 700 "$rehearsal_root/home" "$rehearsal_root/workspace"
chmod 600 "$rehearsal_root/home/auth.json"

printf '\033[2J\033[H'
printf '\033[1;36mdemo-project\033[0m  \033[2mwebsite-driven onboarding\033[0m\n\n'
printf '\033[1;32m›\033[0m Set up AIZK for this project from\n'
printf '  https://xpc3f5mjuv5edgnsfsfbffcoda0ckvij.lambda-url.ap-southeast-1.on.aws\n\n'
printf '\033[2mIsolated read-only container with a disposable project workspace\033[0m\n\n'
sleep 2

docker run --rm --interactive --tty \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 256 \
  --memory 1g \
  --cpus 2 \
  -e TERM=xterm-256color \
  -v "$rehearsal_root/workspace:/workspace:rw" \
  -v "$rehearsal_root/home:/home/node/.codex:rw" \
  -v "$rehearsal_root/home/auth.json:/home/node/.codex/auth.json:ro" \
  craizk-codex-rehearsal:0.147.0 \
  exec \
  --model gpt-5.6-luna \
  --dangerously-bypass-approvals-and-sandbox \
  --ephemeral \
  --skip-git-repo-check \
  "Set up AIZK for this project from https://xpc3f5mjuv5edgnsfsfbffcoda0ckvij.lambda-url.ap-southeast-1.on.aws. This is an isolated recording take, so complete the files and configuration but stop before starting interactive browser login. Report what the user should do next." \
  2>&1 | sed -u -e '/sandbox/d' -e '/session.*id/d'

printf '\n\033[1;36mFiles created\033[0m\n\n'
find "$rehearsal_root/workspace" -maxdepth 5 -type f -printf '  ✓ %P\n' | sort
printf '\n\033[1;32mNext\033[0m  Complete browser sign-in or account creation when Codex asks.\n'
sleep 7
