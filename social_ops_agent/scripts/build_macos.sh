#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
tools_dir="$(cd "$project_dir/.." && pwd)"
cd "$project_dir"

python_bin="$project_dir/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "Missing .venv. Create it and install social_content_crawler plus this package first." >&2
  exit 1
fi

"$python_bin" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name SocialAgent \
  --osx-bundle-identifier com.socialagent.client \
  --paths src \
  --paths "$tools_dir/social_content_crawler/src" \
  --paths "$tools_dir/media_content_analyzer/src" \
  --collect-all cv2 \
  --collect-all imageio_ffmpeg \
  --collect-all playwright \
  --collect-all yt_dlp \
  --collect-submodules social_ops_agent \
  --collect-submodules social_content_crawler \
  --collect-submodules media_content_analyzer \
  desktop_main.py

analyzer_dir="$tools_dir/media_content_analyzer"
"$analyzer_dir/scripts/build_video_repair_worker_macos.sh"
app_path="$project_dir/dist/SocialAgent.app"
worker_target="$app_path/Contents/Resources/video-repair-worker"
mkdir -p "$worker_target"
ditto "$analyzer_dir/build/video-repair-worker-dist/VideoRepairWorker" "$worker_target"
codesign --force --deep --sign - "$app_path"

echo "Built: $app_path"
