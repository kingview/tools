from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from media_content_analyzer import (
    AnalyzeContentInput,
    ArtifactRef,
    InMemoryAnalysisCache,
    InMemoryAuditSink,
    MediaContentAnalyzerTool,
    TOOL_SPEC,
    ToolContext,
)
from media_content_analyzer.contracts import AssetAnalysis, ContentAnalysisOutput
from media_content_analyzer.errors import AnalyzerError, ErrorCode


class FakeBackend:
    pipeline_version = "test-pipeline-v1"

    def __init__(self, *, warnings: list[str] | None = None) -> None:
        self.calls = 0
        self.warnings = warnings or []

    def analyze(self, request, artifacts, work_directory):
        self.calls += 1
        return ContentAnalysisOutput(
            language="zh",
            summary="测试摘要",
            tags=[],
            topics=[],
            entities=[],
            claims=[],
            sentiment="neutral",
            safety_flags=[],
            confidence=0.8,
            evidence=[],
            needs_human_review=False,
            assets=[
                AssetAnalysis(
                    artifact_sha256=request.artifacts[0].sha256,
                    media_type="image/jpeg",
                    modality="image",
                )
            ],
            warnings=self.warnings,
            pipeline_version=self.pipeline_version,
            model_versions={},
        )


def _manifest(path: Path) -> ArtifactRef:
    data = path.read_bytes()
    return ArtifactRef(
        path=str(path),
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        media_type="image/jpeg",
    )


def _context() -> ToolContext:
    return ToolContext(
        tenant_id="tenant-1",
        trace_id="trace-1",
        actor_type="agent",
        actor_id="agent-1",
    )


def test_tool_spec_is_agent_safe_analysis_tool() -> None:
    assert TOOL_SPEC.name == "media.analyze_content"
    assert TOOL_SPEC.category == "analysis"
    assert TOOL_SPEC.side_effect is False
    assert TOOL_SPEC.requires_approval is False
    assert TOOL_SPEC.idempotent is True


def test_tool_validates_manifest_and_uses_cache(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    media_root.mkdir()
    image = media_root / "post.jpg"
    image.write_bytes(b"image-content")
    backend = FakeBackend()
    audit = InMemoryAuditSink()
    tool = MediaContentAnalyzerTool(
        backend=backend,
        audit_sink=audit,
        cache=InMemoryAnalysisCache(),
        allowed_media_root=media_root,
        work_root=tmp_path / "work",
    )
    request = AnalyzeContentInput(artifacts=[_manifest(image)])

    first = asyncio.run(tool.execute(request, _context()))
    second = asyncio.run(tool.execute(request, _context()))

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert backend.calls == 1
    assert [event.event_type for event in audit.events] == [
        "tool.succeeded",
        "tool.succeeded",
    ]


def test_tool_does_not_cache_transient_semantic_model_fallback(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    media_root.mkdir()
    image = media_root / "post.jpg"
    image.write_bytes(b"image-content")
    backend = FakeBackend(
        warnings=[
            "Semantic model failed; deterministic fallback used (ConnectError)."
        ]
    )
    tool = MediaContentAnalyzerTool(
        backend=backend,
        audit_sink=InMemoryAuditSink(),
        cache=InMemoryAnalysisCache(),
        allowed_media_root=media_root,
        work_root=tmp_path / "work",
    )
    request = AnalyzeContentInput(artifacts=[_manifest(image)])

    first = asyncio.run(tool.execute(request, _context()))
    second = asyncio.run(tool.execute(request, _context()))

    assert first.cache_hit is False
    assert second.cache_hit is False
    assert backend.calls == 2


def test_tool_rejects_file_outside_media_root(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    media_root.mkdir()
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"secret")
    audit = InMemoryAuditSink()
    tool = MediaContentAnalyzerTool(
        backend=FakeBackend(),
        audit_sink=audit,
        cache=InMemoryAnalysisCache(),
        allowed_media_root=media_root,
        work_root=tmp_path / "work",
    )

    with pytest.raises(AnalyzerError) as captured:
        asyncio.run(
            tool.execute(AnalyzeContentInput(artifacts=[_manifest(outside)]), _context())
        )

    assert captured.value.code == ErrorCode.INVALID_ARTIFACT
    assert audit.events[-1].event_type == "tool.failed"


def test_tool_rejects_hash_mismatch(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    media_root.mkdir()
    image = media_root / "post.jpg"
    image.write_bytes(b"image-content")
    manifest = _manifest(image).model_copy(update={"sha256": "0" * 64})
    tool = MediaContentAnalyzerTool(
        backend=FakeBackend(),
        audit_sink=InMemoryAuditSink(),
        cache=InMemoryAnalysisCache(),
        allowed_media_root=media_root,
        work_root=tmp_path / "work",
    )

    with pytest.raises(AnalyzerError) as captured:
        asyncio.run(
            tool.execute(AnalyzeContentInput(artifacts=[manifest]), _context())
        )

    assert captured.value.code == ErrorCode.HASH_MISMATCH
