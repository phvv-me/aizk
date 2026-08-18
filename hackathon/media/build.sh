#!/usr/bin/env bash
set -euo pipefail

media_root="hackathon/media"
architecture_source="$media_root/architecture.svg"
cspann_source="$media_root/cockroachdb-cspann.svg"
aws_source="$media_root/aws-operations.svg"
thumbnail_source="$media_root/thumbnail.svg"

mkdir -p "$media_root"

ffmpeg -hide_banner -loglevel error -y \
  -i "$architecture_source" \
  -vf "scale=1800:1200" \
  "$media_root/00-architecture.png"

ffmpeg -hide_banner -loglevel error -y \
  -i "$cspann_source" \
  -vf "scale=1800:1200" \
  -q:v 2 "$media_root/10-cockroachdb-cspann.jpg"

ffmpeg -hide_banner -loglevel error -y \
  -i "$aws_source" \
  -vf "scale=1800:1200" \
  -q:v 2 "$media_root/11-aws-operations.jpg"

ffmpeg -hide_banner -loglevel error -y \
  -i "$thumbnail_source" \
  -i "docs/public/brain-box.webp" \
  -filter_complex "[0:v]scale=1800:1200[base];[1:v]scale=690:690[mark];[base][mark]overlay=995:205" \
  "$media_root/thumbnail-devpost.png"

gallery=(
  "$media_root/01-live-landing.jpg"
  "$media_root/02-product-overview.jpg"
  "$media_root/03-scoped-sharing.jpg"
  "$media_root/10-cockroachdb-cspann.jpg"
  "$media_root/11-aws-operations.jpg"
)

for image in "${gallery[@]}"
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
