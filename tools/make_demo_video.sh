#!/usr/bin/env bash
# Render docs/demo.mp4 from the same recording as docs/demo.svg: one Chromium screenshot per screen change,
# then ffmpeg holds each frame for its real duration. Needs chromium (or chromium-browser / google-chrome) and ffmpeg.
# FFMPEG="nice ffmpeg" overrides the encoder command. Typing animation is not reproduced (commands appear whole). Usage: tools/make_demo_video.sh [chromium-cmd...]
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
FR=$(mktemp -d "$ROOT/.frames.XXXXXX"); trap 'rm -rf "$FR"' EXIT
BROWSER=("$@"); [ ${#BROWSER[@]} -gt 0 ] || BROWSER=(chromium --headless=new --disable-gpu)
python3 "$ROOT/tools/make_demo_svg.py" --from-recording --frames "$FR"
read -r W H < <(grep -o 'width="[0-9]*" height="[0-9]*"' "$FR/f000.svg" | head -1 | tr -dc '0-9 ' | awk '{print $1, $2}')
for f in "$FR"/f*.svg; do
  "${BROWSER[@]}" --hide-scrollbars --force-device-scale-factor=2 --window-size="$W,$H" \
    --screenshot="${f%.svg}.png" "file://$f" >/dev/null 2>&1
  [ -s "${f%.svg}.png" ] || { echo "screenshot failed: $f" >&2; exit 1; }
done
${FFMPEG:-ffmpeg} -loglevel error -y -f concat -i "$FR/list.ffconcat" -vf "fps=25,format=yuv420p" -c:v libx264 -crf 20 \
  -movflags +faststart "$ROOT/docs/demo.mp4"
echo "$ROOT/docs/demo.mp4"
