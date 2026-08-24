from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from .contracts import (
    AnalyzeContentInput,
    AuditEvent,
    ContentAnalysisOutput,
    GeneratePostCopyInput,
    GeneratePostCopyOutput,
    TranscriptSegment,
)


@dataclass(frozen=True, slots=True)
class ToolContext:
    tenant_id: str
    trace_id: str
    actor_type: str
    actor_id: str
    workflow_run_id: str | None = None
    agent_run_id: str | None = None


class AuditSink(Protocol):
    async def record(self, event: AuditEvent) -> None: ...


class AnalysisCache(Protocol):
    def get(self, key: str) -> ContentAnalysisOutput | None: ...

    def put(self, key: str, value: ContentAnalysisOutput) -> None: ...


class AnalysisBackend(Protocol):
    pipeline_version: str

    def analyze(
        self,
        request: AnalyzeContentInput,
        artifacts: Sequence[Path],
        work_directory: Path,
    ) -> ContentAnalysisOutput: ...


class OcrEngine(Protocol):
    name: str

    def extract(self, image_path: Path) -> list[str]: ...


class Transcriber(Protocol):
    name: str

    def transcribe(
        self, audio_path: Path, language_hint: str | None
    ) -> list[TranscriptSegment]: ...


@dataclass(frozen=True, slots=True)
class SemanticResult:
    language: str
    summary: str
    topics: list[str]
    entities: list[str]
    claims: list[str]
    image_summary: str | None
    video_summary: str | None
    transcript_summary: str | None
    sentiment: str
    commercial_intent: str | None
    safety_flags: list[str]
    confidence: float
    evidence_refs: list[str]


class VisionModel(Protocol):
    name: str

    def understand(
        self,
        *,
        images: Sequence[Path],
        trusted_context: str,
        untrusted_content: str,
        language_hint: str | None,
    ) -> SemanticResult | None: ...


class CopyGenerator(Protocol):
    name: str

    def generate(self, request: GeneratePostCopyInput) -> GeneratePostCopyOutput: ...
