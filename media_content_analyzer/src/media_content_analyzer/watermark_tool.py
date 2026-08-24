from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, Sequence

from .contracts import (
    ArtifactRef,
    AuditEvent,
    ProcessWatermarkInput,
    ProcessWatermarkOutput,
    ToolSpec,
    WatermarkMode,
)
from .errors import AnalyzerError, ErrorCode
from .ports import AuditSink, ToolContext


WATERMARK_TOOL_SPEC = ToolSpec(
    name="media.process_watermark",
    version="1.4.3",
    description="Detect static and recurring moving video watermarks, reconstruct overlay pixels with local temporal repair or a portable CoreML/CUDA inpainting worker, and create authorized derivatives without overwriting the original.",
    input_schema=ProcessWatermarkInput.model_json_schema(),
    output_schema=ProcessWatermarkOutput.model_json_schema(),
    category="analysis",
    side_effect=False,
    risk_level="medium",
    timeout_seconds=3_600,
    max_retries=1,
    idempotent=True,
    supports_dry_run=True,
    required_permissions=["media.analyze", "media.transform"],
    policy_tags=[
        "authorization-required-for-removal",
        "preserve-original",
        "local-artifact-only",
        "audited",
    ],
    rate_limit_bucket="media-watermark-processing",
    requires_approval=True,
)


class WatermarkBackend(Protocol):
    detector_version: str

    def process(
        self,
        request: ProcessWatermarkInput,
        artifacts: Sequence[Path],
        output_directory: Path,
    ) -> ProcessWatermarkOutput: ...


class MediaWatermarkProcessorTool:
    def __init__(
        self,
        *,
        backend: WatermarkBackend,
        audit_sink: AuditSink,
        allowed_media_root: Path,
        output_root: Path,
    ) -> None:
        self._backend = backend
        self._audit_sink = audit_sink
        self._allowed_media_root = allowed_media_root.expanduser().resolve()
        self._output_root = output_root.expanduser().resolve()
        self._output_root.mkdir(parents=True, exist_ok=True)

    @property
    def spec(self) -> ToolSpec:
        return WATERMARK_TOOL_SPEC

    async def execute(
        self,
        request: ProcessWatermarkInput,
        context: ToolContext,
    ) -> ProcessWatermarkOutput:
        input_hash = _hash_model(request)
        output: ProcessWatermarkOutput | None = None
        error: AnalyzerError | None = None
        output_directory: Path | None = None
        try:
            if request.mode is WatermarkMode.REMOVE_IF_PRESENT:
                if not request.authorization_confirmed:
                    raise AnalyzerError(
                        ErrorCode.AUTHORIZATION_REQUIRED,
                        "watermark removal requires explicit authorization confirmation",
                    )
            artifacts = await asyncio.to_thread(self._validate_artifacts, request)
            output_directory = self._create_output_directory(context, input_hash)
            output = await asyncio.to_thread(
                self._backend.process,
                request,
                artifacts,
                output_directory,
            )
            if output.processed_count == 0:
                shutil.rmtree(output_directory, ignore_errors=True)
            return output
        except AnalyzerError as exc:
            error = exc
            if output_directory is not None:
                shutil.rmtree(output_directory, ignore_errors=True)
            raise
        except Exception as exc:
            error = AnalyzerError(
                ErrorCode.WATERMARK_DETECTION_FAILED,
                "unexpected watermark processing failure",
            )
            if output_directory is not None:
                shutil.rmtree(output_directory, ignore_errors=True)
            raise error from exc
        finally:
            await self._audit_sink.record(
                AuditEvent(
                    tenant_id=context.tenant_id,
                    trace_id=context.trace_id,
                    workflow_run_id=context.workflow_run_id,
                    agent_run_id=context.agent_run_id,
                    actor_type=context.actor_type,
                    actor_id=context.actor_id,
                    event_type="tool.failed" if error else "tool.succeeded",
                    tool_name=WATERMARK_TOOL_SPEC.name,
                    tool_version=WATERMARK_TOOL_SPEC.version,
                    input_hash=input_hash,
                    output_hash=_hash_model(output) if output else None,
                    error_code=str(error.code) if error else None,
                    created_at=datetime.now(UTC),
                )
            )

    def _validate_artifacts(self, request: ProcessWatermarkInput) -> list[Path]:
        paths: list[Path] = []
        total_size = 0
        for artifact in request.artifacts:
            path = Path(artifact.path).expanduser()
            if not path.is_absolute():
                path = self._allowed_media_root / path
            try:
                resolved = path.resolve(strict=True)
            except (FileNotFoundError, OSError) as exc:
                raise AnalyzerError(ErrorCode.INVALID_ARTIFACT, "media artifact does not exist") from exc
            if not resolved.is_relative_to(self._allowed_media_root):
                raise AnalyzerError(
                    ErrorCode.INVALID_ARTIFACT,
                    "media artifact is outside the executor-managed media root",
                )
            if path.is_symlink() or not resolved.is_file():
                raise AnalyzerError(ErrorCode.INVALID_ARTIFACT, "media artifact must be a regular file")
            if resolved.stat().st_size != artifact.size_bytes:
                raise AnalyzerError(ErrorCode.INVALID_ARTIFACT, "artifact size does not match manifest")
            if _sha256(resolved).lower() != artifact.sha256.lower():
                raise AnalyzerError(ErrorCode.HASH_MISMATCH, "artifact hash does not match manifest")
            total_size += resolved.stat().st_size
            paths.append(resolved)
        if total_size > request.max_total_size_mb * 1024 * 1024:
            raise AnalyzerError(ErrorCode.LIMIT_EXCEEDED, "artifacts exceed total-size limit")
        return paths

    def _create_output_directory(self, context: ToolContext, input_hash: str) -> Path:
        tenant = re.sub(r"[^A-Za-z0-9_.-]", "_", context.tenant_id)[:64] or "tenant"
        destination = (
            self._output_root / tenant / f"{input_hash[:12]}-{uuid.uuid4().hex[:12]}"
        ).resolve()
        if not destination.is_relative_to(self._output_root):
            raise AnalyzerError(ErrorCode.CONFIGURATION_ERROR, "unsafe output directory")
        destination.mkdir(parents=True, exist_ok=False)
        return destination


def _hash_model(value: object) -> str:
    if value is None:
        raw = "null"
    elif hasattr(value, "model_dump_json"):
        raw = value.model_dump_json(exclude_none=False)  # type: ignore[attr-defined]
    else:
        raw = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
