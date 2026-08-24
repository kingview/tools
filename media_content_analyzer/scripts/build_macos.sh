#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_dir"

python_bin="python3"
if [[ -x "$project_dir/.venv/bin/python" ]]; then
  python_bin="$project_dir/.venv/bin/python"
fi

"$python_bin" -c "import PySide6, PyInstaller, paddle, paddleocr, faster_whisper, scenedetect, cv2"

# OpenCV ships its own older OpenSSL libraries. Without explicitly bundling the
# libraries used by this Python interpreter, PyInstaller can bind `_ssl` to the
# OpenCV copies and HTTP clients then fail at runtime with missing X509 symbols.
ssl_module="$($python_bin -c 'import _ssl; print(_ssl.__file__)')"
ssl_library="$(otool -L "$ssl_module" | awk '/\/libssl\.3\.dylib/{gsub(/^[[:space:]]+/, ""); print $1; exit}')"
openssl_args=()
if [[ -n "$ssl_library" ]]; then
  ssl_directory="$(dirname "$ssl_library")"
  if [[ -f "$ssl_directory/libssl.3.dylib" && -f "$ssl_directory/libcrypto.3.dylib" ]]; then
    openssl_args+=(--add-binary "$ssl_directory/libssl.3.dylib:.")
    openssl_args+=(--add-binary "$ssl_directory/libcrypto.3.dylib:.")
  fi
fi

"$python_bin" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name PostInsight \
  --osx-bundle-identifier com.socialagent.postinsight \
  --paths src \
  --collect-all anyio \
  --collect-all h2 \
  --collect-all httpcore \
  --collect-all httpx \
  --collect-all imageio_ffmpeg \
  --collect-all lxml \
  --collect-all modelscope \
  --collect-all modelscope_hub \
  --collect-all pydantic \
  --collect-all pydantic_core \
  --collect-all paddleocr \
  --collect-all paddle \
  --collect-data paddlex \
  --collect-all faster_whisper \
  --collect-all ctranslate2 \
  --collect-all huggingface_hub \
  --collect-all tokenizers \
  --collect-all av \
  --collect-all scenedetect \
  --collect-all cv2 \
  --copy-metadata imagesize \
  --copy-metadata pyclipper \
  --copy-metadata pypdfium2 \
  --copy-metadata python-bidi \
  --copy-metadata shapely \
  --collect-submodules media_content_analyzer \
  "${openssl_args[@]}" \
  desktop_main.py

app_path="$project_dir/dist/PostInsight.app"
cv2_openssl_directory="$app_path/Contents/Frameworks/cv2/__dot__dylibs"
if [[ -n "${ssl_directory:-}" && -d "$cv2_openssl_directory" ]]; then
  # OCR imports OpenCV before HTTP/ASR. dyld reuses the first library with a
  # matching install name, so the nested OpenCV copies must match Python too.
  cp "$ssl_directory/libssl.3.dylib" "$cv2_openssl_directory/libssl.3.dylib"
  cp "$ssl_directory/libcrypto.3.dylib" "$cv2_openssl_directory/libcrypto.3.dylib"
  codesign --force --deep --sign - "$app_path"
fi

echo "Built: $app_path"
