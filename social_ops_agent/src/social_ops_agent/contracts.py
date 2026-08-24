from __future__ import annotations

from enum import StrEnum
from math import ceil
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class AgentPlatform(StrEnum):
    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"
    X = "x"


class AgentSource(StrEnum):
    SEARCH = "search"
    USER = "user"
    TIMELINE = "timeline"
    URL = "url"


class AgentView(StrEnum):
    TOP = "top"
    LATEST = "latest"
    MEDIA = "media"
    POSTS = "posts"
    REPLIES = "replies"
    USERS = "users"


class AgentMediaFormat(StrEnum):
    BEST = "best"
    VIDEO = "video"
    AUDIO = "audio"


class AgentPlan(BaseModel):
    """Validated plan produced from a conversation turn.

    The model may propose values, but this schema owns all executable limits.
    """

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=2_000)
    platform: AgentPlatform
    session_ref: str = Field(
        pattern=r"^sess_(?:douyin|xhs|x)_[A-Za-z0-9_-]{20,80}$",
        max_length=96,
    )
    source: AgentSource = AgentSource.SEARCH
    view: AgentView = AgentView.TOP
    query: str | None = Field(default=None, min_length=1, max_length=300)
    user_key: str | None = Field(default=None, min_length=1, max_length=300)
    start_url: HttpUrl | None = None
    limit: int = Field(default=20, ge=1, le=100)
    download: bool = False
    remove_watermark: bool = False
    watermark_minimum_confidence: float = Field(default=0.72, ge=0.5, le=0.99)
    media_format: AgentMediaFormat = AgentMediaFormat.BEST
    download_batch_size: int = Field(default=20, ge=1, le=20)
    max_total_download_mb: int = Field(default=5_000, ge=100, le=20_000)
    max_scrolls: int = Field(default=30, ge=0, le=50)
    tool_call_budget: int = Field(default=10, ge=1, le=20)
    requires_confirmation: bool = True

    @model_validator(mode="after")
    def validate_executable_plan(self) -> AgentPlan:
        prefix = {
            AgentPlatform.DOUYIN: "sess_douyin_",
            AgentPlatform.XIAOHONGSHU: "sess_xhs_",
            AgentPlatform.X: "sess_x_",
        }[self.platform]
        if not self.session_ref.startswith(prefix):
            raise ValueError("session_ref platform does not match plan platform")
        allowed_views = {
            AgentPlatform.X: {
                AgentSource.SEARCH: {AgentView.TOP, AgentView.LATEST, AgentView.MEDIA},
                AgentSource.USER: {AgentView.POSTS, AgentView.MEDIA, AgentView.REPLIES},
                AgentSource.TIMELINE: {AgentView.LATEST},
                AgentSource.URL: {AgentView.LATEST},
            },
            AgentPlatform.DOUYIN: {
                AgentSource.SEARCH: {AgentView.TOP, AgentView.MEDIA, AgentView.USERS},
                AgentSource.USER: {AgentView.POSTS},
                AgentSource.TIMELINE: {AgentView.TOP},
                AgentSource.URL: {AgentView.TOP},
            },
            AgentPlatform.XIAOHONGSHU: {
                AgentSource.SEARCH: {AgentView.TOP, AgentView.LATEST, AgentView.MEDIA},
                AgentSource.USER: {AgentView.POSTS},
                AgentSource.TIMELINE: {AgentView.TOP},
                AgentSource.URL: {AgentView.TOP},
            },
        }
        if self.view not in allowed_views[self.platform][self.source]:
            raise ValueError("view is not supported for this platform and source")
        if self.source is AgentSource.SEARCH and not self.query:
            raise ValueError("query is required for search")
        if self.source is AgentSource.USER and not self.user_key:
            raise ValueError("user_key is required for user browsing")
        if self.source is AgentSource.URL and self.start_url is None:
            raise ValueError("start_url is required for URL browsing")
        if self.remove_watermark and not self.download:
            raise ValueError("watermark removal requires downloading media first")
        if self.start_url is not None:
            host = (urlsplit(str(self.start_url)).hostname or "").lower()
            domains = {
                AgentPlatform.DOUYIN: ("douyin.com", "iesdouyin.com"),
                AgentPlatform.XIAOHONGSHU: ("xiaohongshu.com", "xhslink.com"),
                AgentPlatform.X: ("x.com", "twitter.com"),
            }[self.platform]
            if not any(host == domain or host.endswith(f".{domain}") for domain in domains):
                raise ValueError("start_url platform does not match plan platform")
        batches = ceil(self.limit / self.download_batch_size) if self.download else 0
        required_calls = 1 + batches + (batches if self.remove_watermark else 0)
        if required_calls > self.tool_call_budget:
            raise ValueError("tool_call_budget is too small for this plan")
        return self


class AgentProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    completed: int = Field(ge=0)
    total: int = Field(ge=1)
    message: str


class AgentRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: AgentPlan
    discovered_urls: list[HttpUrl]
    downloaded_items: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    watermark_detected_count: int = Field(default=0, ge=0)
    watermark_processed_count: int = Field(default=0, ge=0)
    output_directories: list[str] = Field(default_factory=list)
    watermark_output_directories: list[str] = Field(default_factory=list)
    tool_calls_used: int = Field(ge=0)
    cancelled: bool = False
    warnings: list[str] = Field(default_factory=list)
