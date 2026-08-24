"""Public package API with lazy loading for optional media dependencies."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .contracts import (
    AnalyzeContentInput,
    ArtifactRef,
    AssetAnalysis,
    ContentAnalysisOutput,
    CopyPlatform,
    CopyTone,
    Evidence,
    GeneratePostCopyInput,
    GeneratePostCopyOutput,
    GeneratedPostCopy,
    ProcessWatermarkInput,
    ProcessWatermarkOutput,
    ProcessedWatermarkArtifact,
    Tag,
    TagNamespace,
    TranscriptSegment,
    WatermarkArtifactResult,
    WatermarkKind,
    WatermarkMode,
    WatermarkRepairQuality,
    WatermarkRegion,
)
from .ports import SemanticResult, ToolContext


_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "DEFAULT_OLLAMA_BASE_URL": (".adapters", "DEFAULT_OLLAMA_BASE_URL"),
    "DEFAULT_OLLAMA_MODEL": (".adapters", "DEFAULT_OLLAMA_MODEL"),
    "FasterWhisperTranscriber": (".adapters", "FasterWhisperTranscriber"),
    "NoopOcrEngine": (".adapters", "NoopOcrEngine"),
    "NoopTranscriber": (".adapters", "NoopTranscriber"),
    "NoopVisionModel": (".adapters", "NoopVisionModel"),
    "OpenAICompatibleVisionModel": (".adapters", "OpenAICompatibleVisionModel"),
    "OpenAICompatibleCopyGenerator": (".adapters", "OpenAICompatibleCopyGenerator"),
    "PaddleOcrEngine": (".adapters", "PaddleOcrEngine"),
    "InMemoryAuditSink": (".audit", "InMemoryAuditSink"),
    "JsonLinesAuditSink": (".audit", "JsonLinesAuditSink"),
    "LocalMediaAnalysisBackend": (".pipeline", "LocalMediaAnalysisBackend"),
    "InMemoryAnalysisCache": (".runtime", "InMemoryAnalysisCache"),
    "JsonFileAnalysisCache": (".runtime", "JsonFileAnalysisCache"),
    "build_local_tool": (".runtime", "build_local_tool"),
    "build_local_copy_tool": (".runtime", "build_local_copy_tool"),
    "COPY_TOOL_SPEC": (".copy_tool", "COPY_TOOL_SPEC"),
    "ContentCopyGeneratorTool": (".copy_tool", "ContentCopyGeneratorTool"),
    "TOOL_SPEC": (".tool", "TOOL_SPEC"),
    "MediaContentAnalyzerTool": (".tool", "MediaContentAnalyzerTool"),
    "OpenCvWatermarkBackend": (".watermark_processor", "OpenCvWatermarkBackend"),
    "CommandVideoRepairBackend": (".video_repair", "CommandVideoRepairBackend"),
    "HighQualityVideoRepairBackend": (".video_repair", "HighQualityVideoRepairBackend"),
    "WATERMARK_TOOL_SPEC": (".watermark_tool", "WATERMARK_TOOL_SPEC"),
    "MediaWatermarkProcessorTool": (".watermark_tool", "MediaWatermarkProcessorTool"),
    "build_local_watermark_tool": (".watermark_runtime", "build_local_watermark_tool"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_IMPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_IMPORTS))


__all__ = [
    "AnalyzeContentInput",
    "ArtifactRef",
    "AssetAnalysis",
    "ContentAnalysisOutput",
    "ContentCopyGeneratorTool",
    "CommandVideoRepairBackend",
    "COPY_TOOL_SPEC",
    "CopyPlatform",
    "CopyTone",
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_OLLAMA_MODEL",
    "Evidence",
    "GeneratePostCopyInput",
    "GeneratePostCopyOutput",
    "GeneratedPostCopy",
    "FasterWhisperTranscriber",
    "InMemoryAnalysisCache",
    "InMemoryAuditSink",
    "HighQualityVideoRepairBackend",
    "JsonFileAnalysisCache",
    "JsonLinesAuditSink",
    "LocalMediaAnalysisBackend",
    "MediaContentAnalyzerTool",
    "MediaWatermarkProcessorTool",
    "NoopOcrEngine",
    "NoopTranscriber",
    "NoopVisionModel",
    "OpenAICompatibleVisionModel",
    "OpenAICompatibleCopyGenerator",
    "PaddleOcrEngine",
    "ProcessWatermarkInput",
    "ProcessWatermarkOutput",
    "ProcessedWatermarkArtifact",
    "SemanticResult",
    "TOOL_SPEC",
    "Tag",
    "TagNamespace",
    "ToolContext",
    "TranscriptSegment",
    "WATERMARK_TOOL_SPEC",
    "WatermarkArtifactResult",
    "WatermarkKind",
    "WatermarkMode",
    "WatermarkRepairQuality",
    "WatermarkRegion",
    "OpenCvWatermarkBackend",
    "build_local_tool",
    "build_local_copy_tool",
    "build_local_watermark_tool",
]
