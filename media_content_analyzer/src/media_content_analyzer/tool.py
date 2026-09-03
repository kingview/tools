from __future__ import annotations

from .diagnostics import logged

import asyncio
import hashlib
import json
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from .contracts import AnalyzeContentInput, AuditEvent, ContentAnalysisOutput, ToolSpec
from .errors import AnalyzerError, ErrorCode
from .ports import AnalysisBackend, AnalysisCache, AuditSink, ToolContext


TOOL_SPEC = ToolSpec(
    name="media.analyze_content",
    version="1.1.2",
    description="Analyze downloaded image, audio, and video artifacts into tags and summaries.",
    input_schema=AnalyzeContentInput.model_json_schema(),
    output_schema=ContentAnalysisOutput.model_json_schema(),
    category="analysis",
    side_effect=False,
    risk_level="low",
    timeout_seconds=1_800,
    max_retries=1,
    idempotent=True,
    supports_dry_run=False,
    required_permissions=["media.analyze"],
    policy_tags=["local-models", "untrusted-media", "structured-output", "audited"],
    rate_limit_bucket="media-analysis",
    requires_approval=False,
)


class MediaContentAnalyzerTool:
    def __init__(
        self,
        *,
        backend: AnalysisBackend,
        audit_sink: AuditSink,
        cache: AnalysisCache,
        allowed_media_root: Path,
        work_root: Path,
    ) -> None:
        self._backend = backend
        self._audit_sink = audit_sink
        self._cache = cache
        self._allowed_media_root = allowed_media_root.expanduser().resolve()
        self._work_root = work_root.expanduser().resolve()
        self._work_root.mkdir(parents=True, exist_ok=True)

    @property
    def spec(self) -> ToolSpec:
        return TOOL_SPEC

    @logged("media-content", "media.analyze_content")
    async def execute(
        self, request: AnalyzeContentInput, context: ToolContext
    ) -> ContentAnalysisOutput:
        input_hash = _hash_model(request)
        output: ContentAnalysisOutput | None = None
        error: AnalyzerError | None = None
        work_directory: Path | None = None
        try:
            artifacts = await asyncio.to_thread(self._validate_artifacts, request)
            cache_key = _cache_key(request, self._backend.pipeline_version)
            if not request.force_reanalyze:
                cached = await asyncio.to_thread(self._cache.get, cache_key)
                if cached is not None:
                    output = cached.model_copy(update={"cache_hit": True})
                    return output

            work_directory = self._create_work_directory(context, input_hash)
            output = await asyncio.to_thread(
                self._backend.analyze, request, artifacts, work_directory
            )
            if _is_cacheable(output):
                await asyncio.to_thread(
                    self._cache.put,
                    cache_key,
                    output.model_copy(update={"cache_hit": False}),
                )
            return output
        except AnalyzerError as exc:
            error = exc
            raise
        except Exception as exc:
            error = AnalyzerError(
                ErrorCode.ANALYSIS_FAILED,
                f"unexpected media analysis failure ({_safe_error_details(exc)})",
                retryable=False,
            )
            raise error from exc
        finally:
            if work_directory is not None:
                shutil.rmtree(work_directory, ignore_errors=True)
            await self._audit_sink.record(
                AuditEvent(
                    tenant_id=context.tenant_id,
                    trace_id=context.trace_id,
                    workflow_run_id=context.workflow_run_id,
                    agent_run_id=context.agent_run_id,
                    actor_type=context.actor_type,
                    actor_id=context.actor_id,
                    event_type="tool.failed" if error else "tool.succeeded",
                    tool_name=TOOL_SPEC.name,
                    tool_version=TOOL_SPEC.version,
                    input_hash=input_hash,
                    output_hash=_hash_model(output) if output else None,
                    error_code=str(error.code) if error else None,
                    created_at=datetime.now(UTC),
                )
            )

    def _validate_artifacts(self, request: AnalyzeContentInput) -> list[Path]:
        paths: list[Path] = []
        total_size = 0
        for artifact in request.artifacts:
            path = Path(artifact.path).expanduser()
            if not path.is_absolute():
                path = self._allowed_media_root / path
            try:
                resolved = path.resolve(strict=True)
            except (FileNotFoundError, OSError) as exc:
                raise AnalyzerError(
                    ErrorCode.INVALID_ARTIFACT, "media artifact does not exist"
                ) from exc
            if not resolved.is_relative_to(self._allowed_media_root):
                raise AnalyzerError(
                    ErrorCode.INVALID_ARTIFACT,
                    "media artifact is outside the executor-managed media root",
                )
            if path.is_symlink() or not resolved.is_file():
                raise AnalyzerError(
                    ErrorCode.INVALID_ARTIFACT, "media artifact must be a regular file"
                )
            stat = resolved.stat()
            if stat.st_size != artifact.size_bytes:
                raise AnalyzerError(
                    ErrorCode.INVALID_ARTIFACT, "media artifact size does not match its manifest"
                )
            actual_hash = _sha256_file(resolved)
            if actual_hash.lower() != artifact.sha256.lower():
                raise AnalyzerError(
                    ErrorCode.HASH_MISMATCH, "media artifact hash does not match its manifest"
                )
            total_size += stat.st_size
            paths.append(resolved)

        if total_size > request.max_total_size_mb * 1024 * 1024:
            raise AnalyzerError(
                ErrorCode.LIMIT_EXCEEDED,
                "media artifacts exceed the configured total-size limit",
            )
        return paths

    def _create_work_directory(self, context: ToolContext, input_hash: str) -> Path:
        tenant = re.sub(r"[^A-Za-z0-9_.-]", "_", context.tenant_id)[:64] or "tenant"
        destination = (
            self._work_root / tenant / f"{input_hash[:12]}-{uuid.uuid4().hex[:12]}"
        ).resolve()
        if not destination.is_relative_to(self._work_root):
            raise AnalyzerError(ErrorCode.CONFIGURATION_ERROR, "unsafe work directory")
        destination.mkdir(parents=True, exist_ok=False)
        return destination


def _safe_error_details(exc: Exception) -> str:
    """Identify contract failures without logging media, raw model JSON or keys."""
    if isinstance(exc, ValidationError):
        fields = []
        for item in exc.errors(include_input=False, include_context=False, include_url=False)[:5]:
            location = ".".join(
                str(part) if isinstance(part, int) or str(part).isidentifier() else "<field>"
                for part in item["loc"]
            )
            fields.append(f"{location}: {item['type']}")
        return f"ValidationError: {'; '.join(fields)}"[:500]
    return type(exc).__name__


def _cache_key(request: AnalyzeContentInput, pipeline_version: str) -> str:
    payload = {
        "artifacts": [
            {"sha256": item.sha256.lower(), "media_type": item.media_type}
            for item in request.artifacts
        ],
        "post_text": request.post_text,
        "source_url": str(request.source_url) if request.source_url else None,
        "language_hint": request.language_hint,
        "generate_summary": request.generate_summary,
        "generate_tags": request.generate_tags,
        "run_ocr": request.run_ocr,
        "transcribe_audio": request.transcribe_audio,
        "run_vision_model": request.run_vision_model,
        "max_video_duration_seconds": request.max_video_duration_seconds,
        "max_keyframes": request.max_keyframes,
        "pipeline_version": pipeline_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _is_cacheable(output: ContentAnalysisOutput) -> bool:
    """Do not persist transient local-model failures as authoritative analysis."""

    return not any(
        warning.startswith("Semantic model failed;") for warning in output.warnings
    )


def _hash_model(value: object) -> str:
    if value is None:
        return hashlib.sha256(b"null").hexdigest()
    if hasattr(value, "model_dump_json"):
        raw = value.model_dump_json(exclude_none=False)  # type: ignore[attr-defined]
    else:
        raw = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
