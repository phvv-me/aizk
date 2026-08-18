#!/usr/bin/env bash
set -euo pipefail

output_root="${1:-hackathon/video/full/raw}"
display_number=90
display_name=":${display_number}"
runner=$(realpath hackathon/video/full-ops-demo.sh)

mkdir -p "$output_root"

timeout --signal=TERM --kill-after=5s 60s \
  docker compose -f src/deploy/cockroachdb/docker-compose.ccloud.yml \
  run --rm ccloud -q -o json cluster list |
  sed -n '/^\[/,$p' |
  jq '[.[] | {name, plan: .plan, state, cloud_provider: .cloud_provider, cockroach_version: .cockroach_version, regions: [.regions[]?.name]}]' \
    > /tmp/craizk-full-ccloud.json

finish() {
  kill "${terminal_pid:-}" "${recorder_pid:-}" "${display_pid:-}" >/dev/null 2>&1 || true
  wait "${terminal_pid:-}" "${recorder_pid:-}" "${display_pid:-}" >/dev/null 2>&1 || true
}
trap finish EXIT

Xvfb "$display_name" -screen 0 1920x1080x24 -nolisten tcp > /tmp/craizk-full-ops-xvfb.log 2>&1 &
display_pid=$!
sleep 1

DISPLAY="$display_name" ffmpeg -hide_banner -loglevel error -y \
  -f x11grab -draw_mouse 0 -framerate 30 -video_size 1920x1080 -i "${display_name}.0" \
  -t 240 -an -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p \
  -movflags +faststart "$output_root/08-cloud-evidence.mp4" &
recorder_pid=$!

DISPLAY="$display_name" dbus-run-session -- gnome-terminal --wait --hide-menubar \
  --geometry=145x39+0+0 --zoom=1.30 -- "$runner" > /tmp/craizk-full-ops.log 2>&1 &
terminal_pid=$!

wait "$terminal_pid"
unset terminal_pid
sleep 1
kill -INT "$recorder_pid" >/dev/null 2>&1 || true
wait "$recorder_pid" || true
unset recorder_pid

test -s "$output_root/08-cloud-evidence.mp4"
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate \
  -show_entries format=duration -of default=noprint_wrappers=1 \
  "$output_root/08-cloud-evidence.mp4"
