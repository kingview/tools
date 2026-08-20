from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class PlatformDefinition:
    key: str
    display_name: str
    domains: tuple[str, ...]
    extractor: str
    content_types: tuple[str, ...]
    support_level: Literal["direct", "best_effort"] = "direct"


PLATFORM_CATALOG: tuple[PlatformDefinition, ...] = (
    PlatformDefinition(
        key="douyin",
        display_name="抖音",
        domains=("douyin.com", "iesdouyin.com"),
        extractor="Douyin",
        content_types=("视频",),
    ),
    PlatformDefinition(
        key="xiaohongshu",
        display_name="小红书",
        domains=("xiaohongshu.com", "xhslink.com", "xhslink.cn"),
        extractor="XiaoHongShu",
        content_types=("视频", "图片"),
    ),
    PlatformDefinition(
        key="bilibili",
        display_name="哔哩哔哩",
        domains=("bilibili.com", "b23.tv"),
        extractor="BiliBili",
        content_types=("视频", "音频"),
    ),
    PlatformDefinition(
        key="weibo",
        display_name="微博",
        domains=("weibo.com",),
        extractor="Weibo",
        content_types=("视频",),
    ),
    PlatformDefinition(
        key="x",
        display_name="X / Twitter",
        domains=("x.com", "twitter.com"),
        extractor="Twitter",
        content_types=("视频", "音频"),
    ),
    PlatformDefinition(
        key="youtube",
        display_name="YouTube",
        domains=("youtube.com", "youtu.be"),
        extractor="Youtube",
        content_types=("视频", "音频", "字幕"),
    ),
    PlatformDefinition(
        key="tiktok",
        display_name="TikTok",
        domains=("tiktok.com",),
        extractor="TikTok",
        content_types=("视频",),
    ),
    PlatformDefinition(
        key="instagram",
        display_name="Instagram",
        domains=("instagram.com",),
        extractor="Instagram",
        content_types=("视频",),
    ),
    PlatformDefinition(
        key="facebook",
        display_name="Facebook",
        domains=("facebook.com", "fb.watch"),
        extractor="Facebook",
        content_types=("视频",),
    ),
    PlatformDefinition(
        key="reddit",
        display_name="Reddit",
        domains=("reddit.com", "redd.it"),
        extractor="Reddit",
        content_types=("视频", "音频"),
    ),
    PlatformDefinition(
        key="twitch",
        display_name="Twitch",
        domains=("twitch.tv",),
        extractor="Twitch",
        content_types=("视频", "直播回放"),
    ),
    PlatformDefinition(
        key="vimeo",
        display_name="Vimeo",
        domains=("vimeo.com",),
        extractor="Vimeo",
        content_types=("视频",),
    ),
    PlatformDefinition(
        key="threads",
        display_name="Threads*",
        domains=("threads.net",),
        extractor="Generic",
        content_types=("嵌入视频",),
        support_level="best_effort",
    ),
)


def default_allowed_domains() -> frozenset[str]:
    return frozenset(domain for platform in PLATFORM_CATALOG for domain in platform.domains)


def supported_platform_label(separator: str = "   ·   ") -> str:
    return separator.join(platform.display_name for platform in PLATFORM_CATALOG)

