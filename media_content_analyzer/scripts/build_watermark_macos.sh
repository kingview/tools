#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"

python_bin="python3"
if [[ -x "$project_dir/.venv/bin/python" ]]; then
  python_bin="$project_dir/.venv/bin/python"
fi

"$python_bin" -c "import PySide6, PyInstaller, cv2, imageio_ffmpeg"
"$project_dir/scripts/build_video_repair_worker_macos.sh"
worker_dist="$project_dir/build/video-repair-worker-dist"

"$python_bin" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name WatermarkStudio \
  --osx-bundle-identifier com.socialagent.watermarkstudio \
  --paths src \
  --collect-all cv2 \
  --collect-all imageio_ffmpeg \
  --collect-all pydantic \
  --collect-all pydantic_core \
  --exclude-module onnxruntime \
  watermark_desktop_main.py

app_path="$project_dir/dist/WatermarkStudio.app"
mkdir -p "$app_path/Contents/Resources/video-repair-worker"
ditto "$worker_dist/VideoRepairWorker" "$app_path/Contents/Resources/video-repair-worker"
app_version="$("$python_bin" -c 'import pathlib, tomllib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')"
if /usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$app_path/Contents/Info.plist" >/dev/null 2>&1; then
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $app_version" "$app_path/Contents/Info.plist"
else
  /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $app_version" "$app_path/Contents/Info.plist"
fi
if /usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$app_path/Contents/Info.plist" >/dev/null 2>&1; then
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $app_version" "$app_path/Contents/Info.plist"
else
  /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $app_version" "$app_path/Contents/Info.plist"
fi
codesign --force --deep --sign - "$app_path"
echo "Built: $app_path"
