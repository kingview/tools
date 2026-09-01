from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from social_content_crawler.contracts import DownloadInput
from social_content_crawler.ports import ToolContext
from social_content_crawler.runtime import InMemoryAuditSink, LocalRateLimiter
from social_content_crawler.tool import TOOL_SPEC, SocialMediaDownloadTool


class FakeBackend:
    def run(self, request: DownloadInput, output_directory: Path):
        if request.mode == "download":
            (output_directory / "Example-42.mp4").write_bytes(b"video-bytes")
        return [
            {
                "__source_url": str(request.urls[0]),
                "webpage_url": str(request.urls[0]),
                "extractor_key": "Example",
                "id": "42",
                "title": "A public post",
                "uploader": "author",
                "upload_date": "20260819",
                "duration": 12.5,
                "view_count": 10,
            }
        ]


class AllowTestUrls:
    def validate(self, url: str, allowed_domains: frozenset[str]) -> None:
        assert url.startswith("https://")
        assert "example.com" in allowed_domains


def _tool(tmp_path: Path, audit: InMemoryAuditSink) -> SocialMediaDownloadTool:
    return SocialMediaDownloadTool(
        backend=FakeBackend(),
        audit_sink=audit,
        rate_limiter=LocalRateLimiter(minimum_interval_seconds=0),
        url_policy=AllowTestUrls(),
        output_root=tmp_path,
        allowed_domains={"example.com"},
    )


def test_download_normalizes_artifact_and_audits(tmp_path: Path) -> None:
    audit = InMemoryAuditSink()
    result = asyncio.run(
        _tool(tmp_path, audit).execute(
            DownloadInput(urls=["https://video.example.com/post/42"]),
            ToolContext(
                tenant_id="tenant/one",
                trace_id="trace-1",
                actor_type="user",
                actor_id="user-1",
            ),
        )
    )

    assert result.items[0].media_id == "42"
    assert result.items[0].upload_date.isoformat() == "2026-08-19"
    assert result.artifacts[0].size_bytes == len(b"video-bytes")
    assert len(result.artifacts[0].sha256) == 64
    assert Path(result.artifacts[0].path).is_relative_to(tmp_path)
    assert result.network_route == "direct"
    assert audit.events[0].event_type == "tool.succeeded"
    assert audit.events[0].tool_version == "1.9.0"


def test_metadata_only_is_dry_run(tmp_path: Path) -> None:
    audit = InMemoryAuditSink()
    result = asyncio.run(
        _tool(tmp_path, audit).execute(
            DownloadInput(
                urls=["https://video.example.com/post/42"],
                mode="metadata_only",
            ),
            ToolContext(
                tenant_id="tenant-one",
                trace_id="trace-2",
                actor_type="agent",
                actor_id="agent-1",
            ),
        )
    )
    assert result.artifacts == []
    assert result.output_directory is None


def test_input_rejects_http_and_credentials() -> None:
    with pytest.raises(ValidationError):
        DownloadInput(urls=["http://example.com/video"])
    with pytest.raises(ValidationError):
        DownloadInput(urls=["https://user:password@example.com/video"])


def test_input_accepts_only_opaque_session_references() -> None:
    request = DownloadInput(
        urls=["https://x.com/author/status/1"],
        session_ref="sess_x_abcdefghijklmnopqrstuvwx",
    )
    assert request.session_ref.startswith("sess_x_")
    with pytest.raises(ValidationError):
        DownloadInput(
            urls=["https://x.com/author/status/1"],
            session_ref="raw-cookie=value",
        )


def test_telegram_channel_scope_requires_one_telegram_session() -> None:
    request = DownloadInput(
        urls=["https://t.me/weme_download"],
        session_ref="sess_telegram_abcdefghijklmnopqrstuvwx",
        telegram_scope="channel",
        telegram_max_messages=5_000,
    )
    assert request.telegram_scope == "channel"
    assert request.telegram_max_messages == 5_000
    with pytest.raises(ValidationError):
        DownloadInput(
            urls=["https://t.me/weme_download"],
            session_ref="sess_x_abcdefghijklmnopqrstuvwx",
            telegram_scope="channel",
        )


def test_tool_spec_is_contract_first() -> None:
    assert TOOL_SPEC.name == "social.download_media"
    assert TOOL_SPEC.category == "read"
    assert TOOL_SPEC.input_schema["additionalProperties"] is False
