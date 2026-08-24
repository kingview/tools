$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectDir

$PythonExe = "python"
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
  $PythonExe = $VenvPython
}

& $PythonExe -c "import PySide6, PyInstaller, paddle, paddleocr, faster_whisper, scenedetect, cv2"

& $PythonExe -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name PostInsight `
  --paths src `
  --collect-all anyio `
  --collect-all h2 `
  --collect-all httpcore `
  --collect-all httpx `
  --collect-all imageio_ffmpeg `
  --collect-all lxml `
  --collect-all modelscope `
  --collect-all modelscope_hub `
  --collect-all pydantic `
  --collect-all pydantic_core `
  --collect-all paddleocr `
  --collect-all paddle `
  --collect-data paddlex `
  --collect-all faster_whisper `
  --collect-all ctranslate2 `
  --collect-all huggingface_hub `
  --collect-all tokenizers `
  --collect-all av `
  --collect-all scenedetect `
  --collect-all cv2 `
  --copy-metadata imagesize `
  --copy-metadata pyclipper `
  --copy-metadata pypdfium2 `
  --copy-metadata python-bidi `
  --copy-metadata shapely `
  --collect-submodules media_content_analyzer `
  desktop_main.py

Write-Host "Built: $ProjectDir\dist\PostInsight\PostInsight.exe"
