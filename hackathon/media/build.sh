#!/usr/bin/env bash
set -euo pipefail

media_root="hackathon/media"
video="$media_root/../video/full/craizk-complete-demo.mp4"
hero="$media_root/../thumbnail.png"
architecture_source="$media_root/architecture.svg"

mkdir -p "$media_root"

render_frame() {
  local timestamp="$1"
  local output="$2"

  ffmpeg -hide_banner -loglevel error -y \
    -ss "$timestamp" -i "$video" -frames:v 1 \
    -vf "scale=1800:1200:force_original_aspect_ratio=decrease,pad=1800:1200:(ow-iw)/2:(oh-ih)/2:color=0xf4e8d0" \
    -q:v 2 "$media_root/$output"
}

ffmpeg -hide_banner -loglevel error -y \
  -i "$hero" \
  -vf "scale=1800:1200:force_original_aspect_ratio=decrease,pad=1800:1200:(ow-iw)/2:(oh-ih)/2:color=0xf4e8d0" \
  -q:v 2 "$media_root/00-hero.jpg"

render_frame "00:00:02" "01-one-action-onboarding.jpg"
render_frame "00:00:27" "02-agent-setup-guide.jpg"
render_frame "00:00:56" "03-agent-configures-aizk.jpg"
render_frame "00:01:20" "04-logto-sign-in.jpg"
render_frame "00:02:25" "05-authenticated-status.jpg"
render_frame "00:02:36" "06-live-memory-write.jpg"
render_frame "00:03:36" "07-grounded-recall.jpg"
render_frame "00:05:58" "08-memory-console.jpg"
render_frame "00:07:02" "09-aws-architecture.jpg"
render_frame "00:08:30" "10-cspann-plan.jpg"
render_frame "00:08:52" "11-lambda-operations.jpg"

ffmpeg -hide_banner -loglevel error -y \
  -i "$architecture_source" \
  -vf "scale=1800:1200" \
  "$media_root/00-architecture.png"

for image in "$media_root"/*.jpg
do
  dimensions=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height \
    -of csv=s=x:p=0 "$image")
  test "$dimensions" = "1800x1200"
  test "$(stat --format='%s' "$image")" -le 5242880

  dimensions=$(file "$image" | sed 's/.*data, //')
  bytes=$(stat --format='%s' "$image")
  printf '%s | %s bytes | %s\n' "$image" "$bytes" "$dimensions"
done

test "$(stat --format='%s' "$media_root/00-architecture.png")" -le 36700160
dimensions=$(file "$media_root/00-architecture.png" | sed 's/.*data, //')
bytes=$(stat --format='%s' "$media_root/00-architecture.png")
printf '%s | %s bytes | %s\n' "$media_root/00-architecture.png" "$bytes" "$dimensions"

thumbnail="$media_root/thumbnail-devpost.png"
test "$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "$thumbnail")" = "1800x1200"
test "$(stat --format='%s' "$thumbnail")" -le 5242880
dimensions=$(file "$thumbnail" | sed 's/.*data, //')
bytes=$(stat --format='%s' "$thumbnail")
printf '%s | %s bytes | %s\n' "$thumbnail" "$bytes" "$dimensions"
