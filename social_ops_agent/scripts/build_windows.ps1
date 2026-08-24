$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ToolsDir = Split-Path -Parent $ProjectDir
Set-Location $ProjectDir

$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
  throw "Missing .venv. Install social_content_crawler plus this package first."
}

& $PythonExe -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name SocialAgent `
  --paths src `
  --paths "$ToolsDir\social_content_crawler\src" `
  --paths "$ToolsDir\media_content_analyzer\src" `
  --collect-all cv2 `
  --collect-all imageio_ffmpeg `
  --collect-all playwright `
  --collect-all yt_dlp `
  --collect-submodules social_ops_agent `
  --collect-submodules social_content_crawler `
  --collect-submodules media_content_analyzer `
  desktop_main.py

$AnalyzerDir = Join-Path $ToolsDir "media_content_analyzer"
& "$AnalyzerDir\scripts\build_video_repair_worker_windows.ps1"
$WorkerTarget = Join-Path $ProjectDir "dist\SocialAgent\video-repair-worker"
New-Item -ItemType Directory -Path $WorkerTarget -Force | Out-Null
Copy-Item -Path "$AnalyzerDir\build\video-repair-worker-dist\VideoRepairWorker\*" -Destination $WorkerTarget -Recurse -Force

Write-Host "Built: $ProjectDir\dist\SocialAgent\SocialAgent.exe"
