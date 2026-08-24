from __future__ import annotations

import importlib.util
import threading
import uuid
from pathlib import Path

from .adapters import (
    DEFAULT_OLLAMA_MODEL,
    FasterWhisperTranscriber,
    NoopOcrEngine,
    NoopTranscriber,
    NoopVisionModel,
    OpenAICompatibleVisionModel,
    OpenAICompatibleCopyGenerator,
    PaddleOcrEngine,
)
from .audit import InMemoryAuditSink, JsonLinesAuditSink
from .contracts import ContentAnalysisOutput
from .pipeline import LocalMediaAnalysisBackend
from .tool import MediaContentAnalyzerTool
from .copy_tool import ContentCopyGeneratorTool
from .watermark_runtime import build_local_watermark_tool


class InMemoryAnalysisCache:
    def __init__(self) -> None:
        self.values: dict[str, ContentAnalysisOutput] = {}

    def get(self, key: str) -> ContentAnalysisOutput | None:
        return self.values.get(key)

    def put(self, key: str, value: ContentAnalysisOutput) -> None:
        self.values[key] = value


class JsonFileAnalysisCache:
    def __init__(self, directory: Path) -> None:
        self._directory = directory.expanduser().resolve()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def get(self, key: str) -> ContentAnalysisOutput | None:
        path = self._path(key)
        try:
            return ContentAnalysisOutput.model_validate_json(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return None

    def put(self, key: str, value: ContentAnalysisOutput) -> None:
        path = self._path(key)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with self._lock:
            temporary.write_text(value.model_dump_json(indent=2), encoding="utf-8")
            temporary.replace(path)

    def _path(self, key: str) -> Path:
        if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
            raise ValueError("invalid cache key")
        return self._directory / f"{key}.json"


def build_local_tool(
    *,
    allowed_media_root: Path,
    state_root: Path,
    enable_ocr: bool = True,
    enable_asr: bool = True,
    enable_vision: bool = True,
    model_base_url: str | None = None,
    model_name: str = DEFAULT_OLLAMA_MODEL,
    model_api_key: str | None = None,
    whisper_model: str = "small",
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
) -> MediaContentAnalyzerTool:
    state_root = state_root.expanduser().resolve()
    state_root.mkdir(parents=True, exist_ok=True)

    ocr = (
        PaddleOcrEngine()
        if enable_ocr and importlib.util.find_spec("paddleocr") is not None
        else NoopOcrEngine()
    )
    transcriber = (
        FasterWhisperTranscriber(model_name=whisper_model)
        if enable_asr and importlib.util.find_spec("faster_whisper") is not None
        else NoopTranscriber()
    )
    vision = None
    if enable_vision and model_base_url:
        vision = OpenAICompatibleVisionModel(
            base_url=model_base_url,
            model=model_name,
            api_key=model_api_key,
        )
    elif enable_vision:
        vision = OpenAICompatibleVisionModel.from_environment(default_model=model_name)
    backend = LocalMediaAnalysisBackend(
        ocr_engine=ocr,
        transcriber=transcriber,
        vision_model=vision or NoopVisionModel(),
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )
    return MediaContentAnalyzerTool(
        backend=backend,
        audit_sink=JsonLinesAuditSink(state_root / "audit.jsonl"),
        cache=JsonFileAnalysisCache(state_root / "cache"),
        allowed_media_root=allowed_media_root,
        work_root=state_root / "work",
    )


def build_local_copy_tool(
    *,
    state_root: Path,
    model_base_url: str | None = None,
    model_name: str = DEFAULT_OLLAMA_MODEL,
    model_api_key: str | None = None,
) -> ContentCopyGeneratorTool:
    state_root = state_root.expanduser().resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    generator = (
        OpenAICompatibleCopyGenerator(
            base_url=model_base_url,
            model=model_name,
            api_key=model_api_key,
        )
        if model_base_url
        else OpenAICompatibleCopyGenerator.from_environment(default_model=model_name)
    )
    return ContentCopyGeneratorTool(
        generator=generator,
        audit_sink=JsonLinesAuditSink(state_root / "audit.jsonl"),
    )
