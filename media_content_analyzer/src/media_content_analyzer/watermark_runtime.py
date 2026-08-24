from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Callable, Sequence

from .audit import JsonLinesAuditSink
from .video_repair import CommandVideoRepairBackend
from .watermark_processor import OpenCvWatermarkBackend
from .watermark_tool import MediaWatermarkProcessorTool


def build_local_watermark_tool(
    *,
    allowed_media_root: Path,
    state_root: Path,
    output_root: Path | None = None,
    ffmpeg_path: str | None = None,
    high_quality_command: Sequence[str] | None = None,
    progress_callback: Callable[[int, str], None] | None = None,
) -> MediaWatermarkProcessorTool:
    state_root = state_root.expanduser().resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    configured_command = high_quality_command
    if configured_command is None:
        raw_command = os.getenv("WATERMARK_HIGH_QUALITY_COMMAND", "").strip()
        configured_command = (
            shlex.split(raw_command) if raw_command else _bundled_worker_command()
        )
    high_quality_backend = (
        CommandVideoRepairBackend(
            configured_command,
            progress_callback=progress_callback,
        )
        if configured_command
        else None
    )
    return MediaWatermarkProcessorTool(
        backend=OpenCvWatermarkBackend(
            ffmpeg_path=ffmpeg_path,
            high_quality_backend=high_quality_backend,
        ),
        audit_sink=JsonLinesAuditSink(state_root / "audit.jsonl"),
        allowed_media_root=allowed_media_root,
        output_root=output_root or state_root / "watermark-output",
    )


def _bundled_worker_command() -> list[str] | None:
    executable = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        helper = executable.parent.parent / "Resources" / "video-repair-worker" / "VideoRepairWorker"
        if helper.is_file():
            return [str(helper)]
    if os.name == "nt":
        helper = executable.parent / "video-repair-worker" / "VideoRepairWorker.exe"
        if helper.is_file():
            return [str(helper)]
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return None
    return [sys.executable, "-m", "media_content_analyzer.video_repair_worker"]
