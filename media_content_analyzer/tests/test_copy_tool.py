from __future__ import annotations

import asyncio

import pytest

from media_content_analyzer import (
    COPY_TOOL_SPEC,
    ContentCopyGeneratorTool,
    CopyPlatform,
    CopyTone,
    GeneratePostCopyInput,
    GeneratePostCopyOutput,
    GeneratedPostCopy,
    InMemoryAuditSink,
    ToolContext,
)
from media_content_analyzer.contracts import ContentAnalysisOutput
from media_content_analyzer.errors import AnalyzerError, ErrorCode


def _analysis(*, safety_flags: list[str] | None = None) -> ContentAnalysisOutput:
    return ContentAnalysisOutput(
        language="zh",
        summary="一段介绍城市夜景的短视频",
        tags=[],
        topics=["城市夜景"],
        entities=[],
        claims=[],
        sentiment="positive",
        safety_flags=safety_flags or [],
        confidence=0.9,
        evidence=[],
        needs_human_review=False,
        assets=[],
        pipeline_version="test",
        model_versions={},
    )


class FakeGenerator:
    name = "fake-generator"

    def generate(self, request: GeneratePostCopyInput) -> GeneratePostCopyOutput:
        return GeneratePostCopyOutput(
            language=request.language,
            platform=request.platform,
            tone=request.tone,
            variants=[
                GeneratedPostCopy(
                    title="今晚去看灯",
                    body="城市亮起来的时候，记得抬头看看。",
                    hashtags=["城市夜景"],
                )
            ],
            model_version=self.name,
        )


def _context() -> ToolContext:
    return ToolContext(
        tenant_id="tenant-1",
        trace_id="trace-copy",
        actor_type="agent",
        actor_id="agent-1",
    )


def test_copy_tool_spec_is_agent_callable_generation() -> None:
    assert COPY_TOOL_SPEC.name == "media.generate_post_copy"
    assert COPY_TOOL_SPEC.category == "generation"
    assert COPY_TOOL_SPEC.side_effect is False
    assert COPY_TOOL_SPEC.requires_approval is False


def test_copy_tool_generates_and_audits() -> None:
    audit = InMemoryAuditSink()
    tool = ContentCopyGeneratorTool(generator=FakeGenerator(), audit_sink=audit)
    request = GeneratePostCopyInput(
        analysis=_analysis(),
        platform=CopyPlatform.XIAOHONGSHU,
        tone=CopyTone.RECOMMENDATION,
    )

    output = asyncio.run(tool.execute(request, _context()))

    assert output.platform == CopyPlatform.XIAOHONGSHU
    assert output.variants[0].hashtags == ["城市夜景"]
    assert audit.events[-1].tool_name == COPY_TOOL_SPEC.name
    assert audit.events[-1].event_type == "tool.succeeded"


def test_suggestive_copy_blocks_minor_or_nonconsensual_context() -> None:
    audit = InMemoryAuditSink()
    tool = ContentCopyGeneratorTool(generator=FakeGenerator(), audit_sink=audit)
    request = GeneratePostCopyInput(
        analysis=_analysis(safety_flags=["涉及未成年人"]),
        tone=CopyTone.SUGGESTIVE,
    )

    with pytest.raises(AnalyzerError) as captured:
        asyncio.run(tool.execute(request, _context()))

    assert captured.value.code == ErrorCode.GENERATION_FAILED
    assert audit.events[-1].event_type == "tool.failed"
