#!/usr/bin/env bash
set -euo pipefail

video_root="${1:-hackathon/video}"
full_root="$video_root/full"
raw_root="$full_root/raw"
mkdir -p "$full_root"

ffmpeg -hide_banner -loglevel error -y \
  -i "$video_root/raw/01-homepage.mp4" \
  -i "$video_root/raw/02-agent-guide.mp4" \
  -i "$video_root/derived/03-codex-setup-fast.mp4" \
  -i "$video_root/raw/04-account-flow.mp4" \
  -i "$raw_root/05-live-agent-workflow.mp4" \
  -i "$raw_root/05b-grounded-examples.mp4" \
  -i "$raw_root/06-authenticated-console.mp4" \
  -i "$raw_root/07-aws-architecture.mp4" \
  -i "$raw_root/08-cloud-evidence.mp4" \
  -filter_complex '[0:v]setpts=PTS-STARTPTS[v0];[1:v]setpts=PTS-STARTPTS[v1];[2:v]setpts=PTS-STARTPTS[v2];[3:v]setpts=PTS-STARTPTS[v3];[4:v]trim=0:147,setpts=PTS-STARTPTS[v4];[5:v]setpts=PTS-STARTPTS[v5];[6:v]trim=0:105,setpts=PTS-STARTPTS[v6];[7:v]setpts=PTS-STARTPTS[v7];[8:v]setpts=PTS-STARTPTS[v8];[v0][v1][v2][v3][v4][v5][v6][v7][v8]concat=n=9:v=1:a=0[out]' \
  -map '[out]' -an -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p \
  -movflags +faststart "$full_root/craizk-complete-demo.mp4"

ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 \
  "$full_root/craizk-complete-demo.mp4"
