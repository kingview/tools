from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class ArtifactRef(BaseModel):
    """Compatible with social.download_media's DownloadedArtifact output."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4_096)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    media_type: str | None = Field(default=None, max_length=255)


class AnalyzeContentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifacts: list[ArtifactRef] = Field(min_length=1, max_length=100)
    post_text: str | None = Field(default=None, max_length=100_000)
    source_url: HttpUrl | None = None
    language_hint: str | None = Field(default=None, max_length=32)

    generate_summary: bool = True
    generate_tags: bool = True
    run_ocr: bool = True
    transcribe_audio: bool = True
    run_vision_model: bool = True

    max_video_duration_seconds: int = Field(default=3_600, ge=1, le=21_600)
    max_keyframes: int = Field(default=24, ge=1, le=120)
    max_total_size_mb: int = Field(default=2_000, ge=1, le=10_000)
    force_reanalyze: bool = False

    @field_validator("language_hint")
    @classmethod
    def normalize_language_hint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class TagNamespace(StrEnum):
    TOPIC = "topic"
    ENTITY = "entity"
    OBJECT = "object"
    FORMAT = "format"
    SENTIMENT = "sentiment"
    COMMERCIAL = "commercial"
    SAFETY = "safety"


class Tag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: TagNamespace
    label: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    kind: Literal["post_text", "ocr", "transcript", "visual", "metadata"]
    artifact_sha256: str | None = None
    text: str | None = Field(default=None, max_length=10_000)
    timestamp_seconds: float | None = Field(default=None, ge=0)
    confidence: float = Field(default=1.0, ge=0, le=1)


class TranscriptSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    text: str = Field(min_length=1, max_length=20_000)


class AssetAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_sha256: str
    media_type: str
    modality: Literal["image", "video", "audio", "unknown"]
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    duration_seconds: float | None = Field(default=None, ge=0)
    perceptual_hash: str | None = None
    ocr_text: list[str] = Field(default_factory=list)
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    sampled_frame_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)


class ContentAnalysisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str
    summary: str
    tags: list[Tag]
    topics: list[str]
    entities: list[str]
    claims: list[str]

    image_summary: str | None = None
    video_summary: str | None = None
    transcript_summary: str | None = None

    sentiment: str
    commercial_intent: str | None = None
    safety_flags: list[str]
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence]
    needs_human_review: bool

    assets: list[AssetAnalysis]
    warnings: list[str] = Field(default_factory=list)
    cache_hit: bool = False
    pipeline_version: str
    model_versions: dict[str, str]


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    category: Literal["read", "analysis", "generation", "external_write", "account_control"]
    side_effect: bool
    risk_level: Literal["low", "medium", "high", "critical"]
    timeout_seconds: int
    max_retries: int
    idempotent: bool
    supports_dry_run: bool
    required_permissions: list[str]
    policy_tags: list[str]
    rate_limit_bucket: str | None
    requires_approval: bool


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    trace_id: str
    workflow_run_id: str | None = None
    agent_run_id: str | None = None
    actor_type: str
    actor_id: str
    event_type: Literal["tool.succeeded", "tool.failed"]
    tool_name: str
    tool_version: str
    input_hash: str
    output_hash: str | None = None
    error_code: str | None = None
    created_at: datetime
