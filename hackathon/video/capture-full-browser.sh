#!/usr/bin/env bash
set -euo pipefail

site_url=https://xpc3f5mjuv5edgnsfsfbffcoda0ckvij.lambda-url.ap-southeast-1.on.aws
output_root="${1:-hackathon/video/full/raw}"
password_root="${CRAIZK_DEMO_LOGTO_ROOT:-$(< /tmp/craizk-demo-logto-root)}"
display_number=91
webdriver_port=4591
display_name=":${display_number}"

mkdir -p "$output_root"

finish() {
  if [[ -n "${session_id:-}" ]]; then
    curl -fsS -X DELETE "http://127.0.0.1:${webdriver_port}/session/${session_id}" >/dev/null 2>&1 || true
  fi
  kill "${recorder_pid:-}" "${driver_pid:-}" "${display_pid:-}" >/dev/null 2>&1 || true
  wait "${recorder_pid:-}" "${driver_pid:-}" "${display_pid:-}" >/dev/null 2>&1 || true
}
trap finish EXIT

wd() {
  curl -fsS -X POST "http://127.0.0.1:${webdriver_port}/session/${session_id}$1" \
    -H 'content-type: application/json' --data "$2"
}

element() {
  wd /element "$(jq -nc --arg value "$1" '{using:"css selector",value:$value}')" |
    jq -r '.value["element-6066-11e4-a52e-4f735466cecf"]'
}

run_script() {
  wd /execute/sync "$(jq -nc --arg script "$1" '{script:$script,args:[]}')" >/dev/null
}

navigate() {
  wd /url "$(jq -nc --arg url "$1" '{url:$url}')" >/dev/null
}

Xvfb "$display_name" -screen 0 1920x1080x24 -nolisten tcp > /tmp/craizk-full-browser-xvfb.log 2>&1 &
display_pid=$!
DISPLAY="$display_name" geckodriver --port "$webdriver_port" > /tmp/craizk-full-browser-gecko.log 2>&1 &
driver_pid=$!

for attempt in {1..30}; do
  curl -fsS "http://127.0.0.1:${webdriver_port}/status" >/dev/null 2>&1 && break
  sleep 0.2
done

response=$(curl -fsS -X POST "http://127.0.0.1:${webdriver_port}/session" \
  -H 'content-type: application/json' \
  --data '{"capabilities":{"alwaysMatch":{"browserName":"firefox","moz:firefoxOptions":{"args":["--width=1920","--height=1080"]}}}}')
session_id=$(jq -r '.value.sessionId // .sessionId' <<< "$response")

navigate "$site_url/app/dashboard/"
sleep 5
button_id=$(element button)
wd "/element/${button_id}/click" '{}' >/dev/null
sleep 5
identifier_id=$(element 'input[name="identifier"]')
password_id=$(element 'input[name="password"]')
submit_id=$(element 'button[type="submit"]')
password=$(< "$password_root/password")
wd "/element/${identifier_id}/value" "$(jq -nc --arg text maya '{text:$text}')" >/dev/null
wd "/element/${password_id}/value" "$(jq -nc --arg text "$password" '{text:$text}')" >/dev/null
unset password
wd "/element/${submit_id}/click" '{}' >/dev/null
sleep 12

DISPLAY="$display_name" ffmpeg -hide_banner -loglevel error -y \
  -f x11grab -draw_mouse 0 -framerate 30 -video_size 1920x1080 -i "${display_name}.0" \
  -t 145 -an -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p \
  -movflags +faststart "$output_root/06-authenticated-console.mp4" &
recorder_pid=$!

sleep 12
run_script 'document.querySelector("pre")?.scrollTo({top: 720, behavior: "smooth"})'
sleep 8
run_script '[...document.querySelectorAll("button")].find((item)=>item.textContent.trim()==="Processing")?.click()'
sleep 14
run_script 'document.querySelector("pre")?.scrollTo({top: 900, behavior: "smooth"})'
sleep 8
run_script '[...document.querySelectorAll("button")].find((item)=>item.textContent.trim()==="Sources")?.click()'
sleep 14
run_script 'document.querySelector("pre")?.scrollTo({top: 1100, behavior: "smooth"})'
sleep 8
run_script '[...document.querySelectorAll("button")].find((item)=>item.textContent.trim()==="Findings")?.click()'
sleep 14
run_script '[...document.querySelectorAll("button")].find((item)=>item.textContent.trim()==="Subjects")?.click()'
sleep 14
run_script '[...document.querySelectorAll("button")].find((item)=>item.textContent.trim()==="Themes")?.click()'
sleep 14
run_script '[...document.querySelectorAll("button")].find((item)=>item.textContent.trim()==="Organizations")?.click()'
sleep 19

wait "$recorder_pid"
unset recorder_pid

navigate "$site_url/docs/dev/run/aws/"
sleep 5
DISPLAY="$display_name" ffmpeg -hide_banner -loglevel error -y \
  -f x11grab -draw_mouse 0 -framerate 30 -video_size 1920x1080 -i "${display_name}.0" \
  -t 55 -an -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p \
  -movflags +faststart "$output_root/07-aws-architecture.mp4" &
recorder_pid=$!
sleep 12
run_script 'window.scrollTo({top: 620, behavior: "smooth"})'
sleep 13
run_script 'window.scrollTo({top: 1250, behavior: "smooth"})'
sleep 13
run_script 'window.scrollTo({top: 1900, behavior: "smooth"})'
sleep 17
wait "$recorder_pid"
unset recorder_pid

for clip in "$output_root/06-authenticated-console.mp4" "$output_root/07-aws-architecture.mp4"; do
  test -s "$clip"
  ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate \
    -show_entries format=duration -of default=noprint_wrappers=1 "$clip"
done
