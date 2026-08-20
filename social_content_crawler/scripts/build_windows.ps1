$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectDir

$PythonExe = "python"
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
  $PythonExe = $VenvPython
}

& $PythonExe -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name PostDrop `
  --paths src `
  --collect-all imageio_ffmpeg `
  --collect-all yt_dlp `
  --collect-submodules social_content_crawler `
  desktop_main.py

Write-Host "Built: $ProjectDir\dist\PostDrop\PostDrop.exe"
