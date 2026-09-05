from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class BrowsePlatform(StrEnum):
    X = "x"
    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"
    TELEGRAM = "telegram"


class BrowseSource(StrEnum):
    SEARCH = "search"
    USER = "user"
    TIMELINE = "timeline"
    URL = "url"


class BrowseView(StrEnum):
    TOP = "top"
    LATEST = "latest"
    MEDIA = "media"
    POSTS = "posts"
    REPLIES = "replies"
    USERS = "users"


class BrowsePostsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: BrowsePlatform
    session_ref: str = Field(
        pattern=r"^sess_(?:x|douyin|xhs|telegram)_[A-Za-z0-9_-]{20,80}$",
        max_length=96,
    )
    source: BrowseSource
    view: BrowseView = BrowseView.TOP
    query: str | None = Field(default=None, min_length=1, max_length=300)
    user_key: str | None = Field(default=None, min_length=1, max_length=300)
    start_url: HttpUrl | None = None
    max_items: int = Field(default=20, ge=1, le=100)
    max_scrolls: int = Field(default=8, ge=0, le=30)
    navigation_timeout_seconds: float = Field(default=30.0, ge=5.0, le=120.0)
    settle_after_scroll_ms: int = Field(default=900, ge=250, le=5_000)

    @model_validator(mode="after")
    def validate_source_fields(self) -> BrowsePostsInput:
        allowed_views = {
            BrowsePlatform.X: {
                BrowseSource.SEARCH: {BrowseView.TOP, BrowseView.LATEST, BrowseView.MEDIA},
                BrowseSource.USER: {BrowseView.POSTS, BrowseView.MEDIA, BrowseView.REPLIES},
                BrowseSource.TIMELINE: {BrowseView.LATEST},
                BrowseSource.URL: {BrowseView.LATEST},
            },
            BrowsePlatform.DOUYIN: {
                BrowseSource.SEARCH: {BrowseView.TOP, BrowseView.MEDIA, BrowseView.USERS},
                BrowseSource.USER: {BrowseView.POSTS},
                BrowseSource.TIMELINE: {BrowseView.TOP},
                BrowseSource.URL: {BrowseView.TOP},
            },
            BrowsePlatform.XIAOHONGSHU: {
                BrowseSource.SEARCH: {BrowseView.TOP, BrowseView.LATEST, BrowseView.MEDIA},
                BrowseSource.USER: {BrowseView.POSTS},
                BrowseSource.TIMELINE: {BrowseView.TOP},
                BrowseSource.URL: {BrowseView.TOP},
            },
            BrowsePlatform.TELEGRAM: {
                BrowseSource.USER: {BrowseView.POSTS, BrowseView.MEDIA, BrowseView.LATEST},
                BrowseSource.URL: {BrowseView.POSTS, BrowseView.MEDIA, BrowseView.LATEST},
            },
        }
        platform_sources = allowed_views[self.platform]
        if self.source not in platform_sources or self.view not in platform_sources[self.source]:
            raise ValueError(
                f"view={self.view} is not valid for platform={self.platform}, source={self.source}"
            )
        if self.source is BrowseSource.SEARCH and not (self.query or "").strip():
            raise ValueError("query is required for search")
        if self.source is BrowseSource.USER and not self.user_key:
            raise ValueError("user_key is required for user browsing")
        if self.user_key and any(character in self.user_key for character in "/?#"):
            raise ValueError("user_key cannot contain URL separators")
        expected_prefix = {
            BrowsePlatform.X: "sess_x_",
            BrowsePlatform.DOUYIN: "sess_douyin_",
            BrowsePlatform.XIAOHONGSHU: "sess_xhs_",
            BrowsePlatform.TELEGRAM: "sess_telegram_",
        }[self.platform]
        if not self.session_ref.startswith(expected_prefix):
            raise ValueError("session_ref platform does not match platform")
        if self.source is BrowseSource.URL:
            if self.start_url is None:
                raise ValueError("start_url is required for URL browsing")
            host = (self.start_url.host or "").lower().rstrip(".")
            allowed_domains = {
                BrowsePlatform.X: {"x.com", "twitter.com"},
                BrowsePlatform.DOUYIN: {"douyin.com", "iesdouyin.com"},
                BrowsePlatform.XIAOHONGSHU: {"xiaohongshu.com", "xhslink.com"},
                BrowsePlatform.TELEGRAM: {"t.me", "telegram.me", "web.telegram.org"},
            }[self.platform]
            if self.start_url.scheme != "https" or not any(
                host == domain or host.endswith(f".{domain}") for domain in allowed_domains
            ):
                raise ValueError("start_url platform does not match platform")
            if self.start_url.username or self.start_url.password:
                raise ValueError("start_url cannot contain credentials")
        return self


class PostMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replies: int | None = None
    reposts: int | None = None
    likes: int | None = None
    views: int | None = None


class BrowsedPost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    post_id: str
    author_id: str | None = None
    author_handle: str | None = None
    author_name: str | None = None
    text: str | None = None
    language: str | None = None
    published_at: datetime | None = None
    media_types: list[Literal["image", "video"]] = Field(default_factory=list)
    metrics: PostMetrics = Field(default_factory=PostMetrics)
    position: int = Field(ge=1)


class BrowsePostsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: BrowsePlatform
    source_url: HttpUrl
    posts: list[BrowsedPost]
    truncated: bool
    next_cursor: str | None = None
    warnings: list[str] = Field(default_factory=list)
    observed_at: datetime
