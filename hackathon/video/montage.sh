#!/usr/bin/env bash
set -euo pipefail

video_root="${1:-hackathon/video}"
raw_root="$video_root/raw"
derived_root="$video_root/derived"

mkdir -p "$derived_root"

ffmpeg -hide_banner -loglevel error -y \
  -i "$raw_root/03-codex-setup.mp4" \
  -an -filter_complex \
  '[0:v]trim=1:6,setpts=PTS-STARTPTS[intro];[0:v]trim=start=40,setpts=0.45*(PTS-STARTPTS),fps=30[work];[intro][work]concat=n=2:v=1:a=0[out]' \
  -map '[out]' \
  -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p -movflags +faststart \
  "$derived_root/03-codex-setup-fast.mp4"

ffmpeg -hide_banner -loglevel error -y \
  -i "$raw_root/01-homepage.mp4" \
  -i "$raw_root/02-agent-guide.mp4" \
  -i "$derived_root/03-codex-setup-fast.mp4" \
  -i "$raw_root/04-account-flow.mp4" \
  -filter_complex '[0:v]trim=0:14,setpts=PTS-STARTPTS[v0];[1:v]trim=0:16,setpts=PTS-STARTPTS[v1];[2:v]setpts=PTS-STARTPTS[v2];[3:v]trim=0:13,setpts=PTS-STARTPTS[v3];[v0][v1][v2][v3]concat=n=4:v=1:a=0[out]' \
  -map '[out]' -an -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p \
  -movflags +faststart "$video_root/rough-cut.mp4"

ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 \
  "$video_root/rough-cut.mp4"
