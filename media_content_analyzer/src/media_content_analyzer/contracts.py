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


class WatermarkMode(StrEnum):
    DETECT_ONLY = "detect_only"
    REMOVE_IF_PRESENT = "remove_if_present"


class WatermarkKind(StrEnum):
    STATIC = "static"
    MOVING = "moving"
    TRANSLUCENT = "translucent"
    UNKNOWN = "unknown"


class WatermarkRepairQuality(StrEnum):
    AUTO = "auto"
    FAST = "fast"
    BALANCED = "balanced"
    HIGH = "high"


class WatermarkRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(ge=3)
    height: int = Field(ge=3)
    confidence: float = Field(default=1.0, ge=0, le=1)
    first_seen_seconds: float | None = Field(default=None, ge=0)
    last_seen_seconds: float | None = Field(default=None, ge=0)


class ProcessWatermarkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifacts: list[ArtifactRef] = Field(min_length=1, max_length=20)
    mode: WatermarkMode = WatermarkMode.DETECT_ONLY
    authorization_confirmed: bool = False
    preserve_original: bool = True
    minimum_confidence: float = Field(default=0.72, ge=0.5, le=0.99)
    sample_frames: int | None = Field(default=None, ge=8, le=240)
    manual_regions: dict[str, list[WatermarkRegion]] = Field(default_factory=dict)
    track_manual_regions: bool = False
    tracking_search_radius: int = Field(default=240, ge=32, le=1_200)
    inpaint_radius: int = Field(default=5, ge=1, le=20)
    repair_quality: WatermarkRepairQuality = WatermarkRepairQuality.AUTO
    temporal_consistency: bool = True
    max_total_size_mb: int = Field(default=5_000, ge=1, le=20_000)

    @field_validator("preserve_original")
    @classmethod
    def require_original_preservation(cls, value: bool) -> bool:
        if not value:
            raise ValueError("original artifacts must always be preserved")
        return value

    @field_validator("authorization_confirmed")
    @classmethod
    def normalize_authorization(cls, value: bool) -> bool:
        return bool(value)

    def validate_removal_authorization(self) -> None:
        if self.mode is WatermarkMode.REMOVE_IF_PRESENT and not self.authorization_confirmed:
            raise ValueError("watermark removal requires explicit authorization confirmation")


class ProcessedWatermarkArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = "video/mp4"
    derived_from_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class WatermarkArtifactResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original: ArtifactRef
    detected: bool
    kind: WatermarkKind | None = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    regions: list[WatermarkRegion] = Field(default_factory=list)
    processed_artifact: ProcessedWatermarkArtifact | None = None
    quality_score: float | None = Field(default=None, ge=0, le=1)
    repair_quality_requested: WatermarkRepairQuality | None = None
    repair_quality_applied: WatermarkRepairQuality | None = None
    repair_method: str | None = Field(default=None, max_length=255)
    needs_human_review: bool = False
    warnings: list[str] = Field(default_factory=list)


class ProcessWatermarkOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[WatermarkArtifactResult]
    detected_count: int = Field(ge=0)
    processed_count: int = Field(ge=0)
    output_directory: str | None = None
    detector_version: str


class AnalyzeContentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifacts: list[ArtifactRef] = Field(min_length=1, max_length=100)
    post_text: str | None = Field(default=None, max_length=100_000)
    source_url: HttpUrl | None = None
    language_hint: str | None = Field(default=None, max_length=32)
    analysis_profile: str | None = Field(default=None, max_length=10000)

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


TAG_LABEL_MAX_LENGTH = 200
TAG_EVIDENCE_MAX_ITEMS = 50


class Tag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: TagNamespace
    label: str = Field(min_length=1, max_length=TAG_LABEL_MAX_LENGTH)
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list, max_length=TAG_EVIDENCE_MAX_ITEMS)


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
    material_features: dict = Field(default_factory=dict)


class CopyPlatform(StrEnum):
    GENERIC = "generic"
    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"
    BILIBILI = "bilibili"
    WEIBO = "weibo"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"


class CopyTone(StrEnum):
    NATURAL = "natural"
    RECOMMENDATION = "recommendation"
    PROFESSIONAL = "professional"
    HUMOROUS = "humorous"
    EMOTIONAL = "emotional"
    SUGGESTIVE = "suggestive"


class GeneratePostCopyInput(BaseModel):
    """Agent/GUI request for copy grounded in a completed analysis."""

    model_config = ConfigDict(extra="forbid")

    analysis: ContentAnalysisOutput
    platform: CopyPlatform = CopyPlatform.GENERIC
    tone: CopyTone = CopyTone.NATURAL
    language: str = Field(default="zh", min_length=2, max_length=32)
    objective: str | None = Field(default=None, max_length=1_000)
    extra_instructions: str | None = Field(default=None, max_length=10_000)
    variant_count: int = Field(default=3, ge=1, le=5)
    max_characters: int = Field(default=300, ge=20, le=5_000)
    include_hashtags: bool = True


class GeneratedPostCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=500)
    body: str = Field(min_length=1, max_length=10_000)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    call_to_action: str | None = Field(default=None, max_length=1_000)


class GeneratePostCopyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str
    platform: CopyPlatform
    tone: CopyTone
    variants: list[GeneratedPostCopy] = Field(min_length=1, max_length=5)
    warnings: list[str] = Field(default_factory=list)
    needs_human_review: bool = False
    model_version: str


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
