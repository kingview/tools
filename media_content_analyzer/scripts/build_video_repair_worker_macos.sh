#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"

python_bin="python3"
if [[ -x "$project_dir/.venv/bin/python" ]]; then
  python_bin="$project_dir/.venv/bin/python"
fi

"$python_bin" -c "import PyInstaller, cv2, imageio_ffmpeg, onnxruntime"
model_path="$("$python_bin" -c 'from media_content_analyzer.video_repair_worker import resolve_model_path; print(resolve_model_path(download=True))')"
worker_dist="$project_dir/build/video-repair-worker-dist"
"$python_bin" -m PyInstaller \
  --noconfirm \
  --clean \
  --console \
  --name VideoRepairWorker \
  --distpath "$worker_dist" \
  --workpath "$project_dir/build/VideoRepairWorker-build" \
  --specpath "$project_dir/build" \
  --paths src \
  --collect-all cv2 \
  --collect-all imageio_ffmpeg \
  --add-data "$model_path:models" \
  --hidden-import onnxruntime \
  --exclude-module onnxruntime.quantization \
  --exclude-module onnxruntime.tools \
  --exclude-module onnxruntime.training \
  --exclude-module onnxruntime.transformers \
  --exclude-module pandas \
  --exclude-module scipy \
  --exclude-module pytest \
  video_repair_worker_main.py

echo "Built: $worker_dist/VideoRepairWorker"
