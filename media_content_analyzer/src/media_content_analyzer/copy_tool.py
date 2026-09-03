from __future__ import annotations

from .diagnostics import logged

import asyncio
import hashlib
import json
from datetime import UTC, datetime

from .contracts import (
    AuditEvent,
    CopyTone,
    GeneratePostCopyInput,
    GeneratePostCopyOutput,
    ToolSpec,
)
from .errors import AnalyzerError, ErrorCode
from .ports import AuditSink, CopyGenerator, ToolContext


COPY_TOOL_SPEC = ToolSpec(
    name="media.generate_post_copy",
    version="1.0.2",
    description="Generate platform-aware social post copy grounded in an analysis result.",
    input_schema=GeneratePostCopyInput.model_json_schema(),
    output_schema=GeneratePostCopyOutput.model_json_schema(),
    category="generation",
    side_effect=False,
    risk_level="medium",
    timeout_seconds=300,
    max_retries=1,
    idempotent=False,
    supports_dry_run=False,
    required_permissions=["media.generate_copy"],
    policy_tags=[
        "local-models",
        "untrusted-content",
        "grounded-generation",
        "audited",
    ],
    rate_limit_bucket="media-copy-generation",
    requires_approval=False,
)


class ContentCopyGeneratorTool:
    def __init__(self, *, generator: CopyGenerator, audit_sink: AuditSink) -> None:
        self._generator = generator
        self._audit_sink = audit_sink

    @property
    def spec(self) -> ToolSpec:
        return COPY_TOOL_SPEC

    @logged("media-content", "media.generate_post_copy")
    async def execute(
        self, request: GeneratePostCopyInput, context: ToolContext
    ) -> GeneratePostCopyOutput:
        input_hash = _hash_model(request)
        output: GeneratePostCopyOutput | None = None
        error: AnalyzerError | None = None
        try:
            _validate_suggestive_request(request)
            output = await asyncio.to_thread(self._generator.generate, request)
            return output
        except AnalyzerError as exc:
            error = exc
            raise
        except Exception as exc:
            error = AnalyzerError(
                ErrorCode.GENERATION_FAILED,
                f"文案生成失败（{type(exc).__name__}）。请确认 Ollama 和模型正在运行。",
                retryable=True,
            )
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
                    tool_name=COPY_TOOL_SPEC.name,
                    tool_version=COPY_TOOL_SPEC.version,
                    input_hash=input_hash,
                    output_hash=_hash_model(output) if output else None,
                    error_code=str(error.code) if error else None,
                    created_at=datetime.now(UTC),
                )
            )


def _validate_suggestive_request(request: GeneratePostCopyInput) -> None:
    if request.tone != CopyTone.SUGGESTIVE:
        return
    text = " ".join(
        [
            *request.analysis.safety_flags,
            request.analysis.summary,
            request.objective or "",
            request.extra_instructions or "",
        ]
    ).lower()
    blocked_markers = (
        "未成年",
        "未满18",
        "儿童",
        "幼女",
        "child",
        "minor",
        "underage",
        "强迫",
        "非自愿",
        "偷拍",
        "胁迫",
        "non-consensual",
        "coercion",
        "voyeur",
    )
    if any(marker in text for marker in blocked_markers):
        raise AnalyzerError(
            ErrorCode.GENERATION_FAILED,
            "暧昧吸睛文案不能用于未成年人、年龄不明、非自愿或偷拍内容。",
            retryable=False,
        )


def _hash_model(value: object) -> str:
    if value is None:
        raw = "null"
    elif hasattr(value, "model_dump_json"):
        raw = value.model_dump_json(exclude_none=False)  # type: ignore[attr-defined]
    else:
        raw = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()
