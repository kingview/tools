#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"

python_bin="python3"
if [[ -x "$project_dir/.venv/bin/python" ]]; then
  python_bin="$project_dir/.venv/bin/python"
fi

"$python_bin" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name PostDrop \
  --osx-bundle-identifier com.socialagent.postdrop \
  --paths src \
  --collect-all imageio_ffmpeg \
  --collect-all playwright \
  --collect-all yt_dlp \
  --collect-submodules social_content_crawler \
  desktop_main.py

echo "Built: $project_dir/dist/PostDrop.app"
