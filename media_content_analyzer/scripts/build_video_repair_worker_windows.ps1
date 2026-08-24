$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectDir

$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
  throw "Missing .venv. Install the repair and build extras first."
}

& $PythonExe -c "import PyInstaller, cv2, imageio_ffmpeg, onnxruntime"
$WorkerDist = Join-Path $ProjectDir "build\video-repair-worker-dist"
& $PythonExe -m PyInstaller `
  --noconfirm `
  --clean `
  --console `
  --name VideoRepairWorker `
  --distpath $WorkerDist `
  --workpath "$ProjectDir\build\VideoRepairWorker-build" `
  --specpath "$ProjectDir\build" `
  --paths src `
  --collect-all cv2 `
  --collect-all imageio_ffmpeg `
  --hidden-import onnxruntime `
  --exclude-module onnxruntime.quantization `
  --exclude-module onnxruntime.tools `
  --exclude-module onnxruntime.training `
  --exclude-module onnxruntime.transformers `
  --exclude-module pandas `
  --exclude-module scipy `
  --exclude-module pytest `
  video_repair_worker_main.py

Write-Host "Built: $WorkerDist\VideoRepairWorker\VideoRepairWorker.exe"
