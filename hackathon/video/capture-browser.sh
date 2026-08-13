#!/usr/bin/env bash
set -euo pipefail

site_url="https://xpc3f5mjuv5edgnsfsfbffcoda0ckvij.lambda-url.ap-southeast-1.on.aws"
output_root="${1:-hackathon/video/raw}"
display_number=96
webdriver_port=4596
display_name=":${display_number}"

mkdir -p "$output_root"

finish() {
  if [[ -n "${session_id:-}" ]]; then
    curl -fsS -X DELETE "http://127.0.0.1:${webdriver_port}/session/${session_id}" >/dev/null 2>&1 || true
  fi
  kill "${driver_pid:-}" "${display_pid:-}" >/dev/null 2>&1 || true
  wait "${driver_pid:-}" "${display_pid:-}" >/dev/null 2>&1 || true
}
trap finish EXIT

navigate() {
  local page_url="$1"
  curl -fsS -X POST "http://127.0.0.1:${webdriver_port}/session/${session_id}/url" \
    -H 'content-type: application/json' \
    --data "$(jq -nc --arg url "$page_url" '{url: $url}')" >/dev/null
}

run_script() {
  local javascript="$1"
  curl -fsS -X POST "http://127.0.0.1:${webdriver_port}/session/${session_id}/execute/sync" \
    -H 'content-type: application/json' \
    --data "$(jq -nc --arg script "$javascript" '{script: $script, args: []}')" >/dev/null
}

record() {
  local seconds="$1"
  local destination="$2"
  DISPLAY="$display_name" ffmpeg -hide_banner -loglevel error -y \
    -f x11grab -draw_mouse 0 -framerate 30 -video_size 1920x1080 -i "${display_name}.0" \
    -t "$seconds" -an -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p \
    -movflags +faststart "$destination" &
  recorder_pid=$!
}

wait_for_recording() {
  wait "$recorder_pid"
  unset recorder_pid
}

Xvfb "$display_name" -screen 0 1920x1080x24 -nolisten tcp > /tmp/aizk-video-xvfb.log 2>&1 &
display_pid=$!
DISPLAY="$display_name" geckodriver --port "$webdriver_port" > /tmp/aizk-video-geckodriver.log 2>&1 &
driver_pid=$!

for attempt in {1..30}; do
  curl -fsS "http://127.0.0.1:${webdriver_port}/status" >/dev/null 2>&1 && break
  sleep 0.2
done

response=$(curl -sS -X POST "http://127.0.0.1:${webdriver_port}/session" \
  -H 'content-type: application/json' \
  --data '{"capabilities":{"alwaysMatch":{"browserName":"firefox","moz:firefoxOptions":{"args":["--width=1920","--height=1080"]}}}}')
session_id=$(jq -r '.value.sessionId // .sessionId' <<< "$response")
if [[ "$session_id" == null ]]; then
  jq . <<< "$response"
  exit 1
fi

navigate "$site_url/"
sleep 4
record 18 "$output_root/01-homepage.mp4"
sleep 5
run_script 'window.scrollTo({top: 780, behavior: "smooth"});'
sleep 6
run_script 'window.scrollTo({top: 1480, behavior: "smooth"});'
sleep 7
wait_for_recording

navigate "$site_url/setup.md"
sleep 3
run_script 'document.body.style.cssText = "max-width: 1400px; margin: 36px auto; padding: 0 48px; font: 22px/1.5 monospace; white-space: pre-wrap; color: #1e1b4b; background: #fff"; window.scrollTo(0, 0);'
record 20 "$output_root/02-agent-guide.mp4"
sleep 4
run_script 'window.scrollTo({top: 700, behavior: "smooth"});'
sleep 5
run_script 'window.scrollTo({top: 1500, behavior: "smooth"});'
sleep 5
run_script 'window.scrollTo({top: 2300, behavior: "smooth"});'
sleep 6
wait_for_recording

navigate "$site_url/app/dashboard/"
sleep 7
record 16 "$output_root/04-account-flow.mp4"
sleep 4
run_script 'const button = [...document.querySelectorAll("button")].find((item) => item.textContent.includes("Create account or sign in")); if (button) button.click();'
sleep 12
wait_for_recording

for clip in "$output_root"/01-homepage.mp4 "$output_root"/02-agent-guide.mp4 "$output_root"/04-account-flow.mp4; do
  test -s "$clip"
  ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate \
    -of default=noprint_wrappers=1 "$clip"
done
