$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectDir

$PythonExe = "python"
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
  $PythonExe = $VenvPython
}

& $PythonExe -c "import PySide6, PyInstaller, cv2, imageio_ffmpeg, huggingface_hub, onnxruntime"

& "$ProjectDir\scripts\build_video_repair_worker_windows.ps1"
$WorkerDist = Join-Path $ProjectDir "build\video-repair-worker-dist"

& $PythonExe -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name WatermarkStudio `
  --paths src `
  --collect-all cv2 `
  --collect-all imageio_ffmpeg `
  --collect-all pydantic `
  --collect-all pydantic_core `
  --exclude-module onnxruntime `
  watermark_desktop_main.py

$WorkerTarget = Join-Path $ProjectDir "dist\WatermarkStudio\video-repair-worker"
New-Item -ItemType Directory -Force -Path $WorkerTarget | Out-Null
Copy-Item -Recurse -Force "$WorkerDist\VideoRepairWorker\*" $WorkerTarget

Write-Host "Built: $ProjectDir\dist\WatermarkStudio\WatermarkStudio.exe"
