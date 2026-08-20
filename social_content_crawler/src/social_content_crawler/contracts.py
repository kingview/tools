from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class DownloadMode(StrEnum):
    METADATA_ONLY = "metadata_only"
    DOWNLOAD = "download"


class MediaFormat(StrEnum):
    BEST = "best"
    VIDEO = "video"
    AUDIO = "audio"


class BrowserCookieSource(StrEnum):
    NONE = "none"
    AUTO = "auto"
    CHROME = "chrome"
    EDGE = "edge"
    FIREFOX = "firefox"
    SAFARI = "safari"


class DownloadInput(BaseModel):
    """Public social-media URLs only; browser sessions are local and optional."""

    model_config = ConfigDict(extra="forbid")

    urls: list[HttpUrl] = Field(min_length=1, max_length=20)
    mode: DownloadMode = DownloadMode.DOWNLOAD
    media_format: MediaFormat = MediaFormat.BEST
    include_playlists: bool = False
    max_items: int = Field(default=20, ge=1, le=100)
    max_file_size_mb: int = Field(default=500, ge=1, le=2_000)
    max_total_size_mb: int = Field(default=1_000, ge=1, le=5_000)
    max_duration_seconds: int = Field(default=3_600, ge=1, le=21_600)
    request_timeout_seconds: float = Field(default=30.0, ge=5.0, le=120.0)
    write_thumbnail: bool = False
    write_subtitles: bool = False
    browser_cookie_source: BrowserCookieSource = BrowserCookieSource.NONE

    @model_validator(mode="after")
    def validate_urls(self) -> DownloadInput:
        for url in self.urls:
            if url.scheme != "https":
                raise ValueError("only HTTPS URLs are allowed")
            if url.username or url.password:
                raise ValueError("URLs containing credentials are not allowed")
        return self


class MediaInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: HttpUrl
    webpage_url: HttpUrl | None = None
    extractor: str
    media_id: str
    title: str | None = None
    description: str | None = None
    uploader: str | None = None
    uploader_id: str | None = None
    upload_date: date | None = None
    duration_seconds: float | None = None
    thumbnail_url: HttpUrl | None = None
    view_count: int | None = None
    like_count: int | None = None
    repost_count: int | None = None
    comment_count: int | None = None


class DownloadedArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    size_bytes: int
    sha256: str
    media_type: str | None = None


class DownloadOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MediaInfo]
    artifacts: list[DownloadedArtifact]
    output_directory: str | None = None


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
