#!/usr/bin/env bash
# Render vanguard.svg -> vanguard.png for Unraid's Docker tab.
#
# Unraid needs a PNG: its icon handling falls back to a question mark for SVG
# and renders nothing at all for WebP, so the SVG here is source only.
#
# Chrome is used rather than ImageMagick because IM without librsvg rasterises
# SVG through its own limited renderer and drops the gradients.
set -euo pipefail

cd "$(dirname "$0")"

chrome=$(command -v google-chrome || command -v google-chrome-stable || command -v chromium || true)
if [ -z "$chrome" ]; then
  echo "need google-chrome or chromium on PATH" >&2
  exit 1
fi

"$chrome" --headless=new --disable-gpu --no-sandbox \
  --default-background-color=00000000 \
  --force-device-scale-factor=1 \
  --window-size=512,512 \
  --screenshot=vanguard.png \
  "file://$PWD/vanguard.svg" 2>/dev/null

echo "wrote $PWD/vanguard.png"
