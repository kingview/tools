from __future__ import annotations

import asyncio
from types import SimpleNamespace

from social_ops_agent import AgentPlan, SocialOperationsAgent
from social_content_crawler.contracts import DownloadedArtifact


SESSION_REF = "sess_douyin_abcdefghijklmnopqrstuvwx"


class FakeBrowseTool:
    def __init__(self, count: int) -> None:
        self.count = count
        self.calls = []

    async def execute(self, request, context):
        self.calls.append(request)
        return SimpleNamespace(
            posts=[
                SimpleNamespace(url=f"https://www.douyin.com/video/{index}")
                for index in range(1, self.count + 1)
            ],
            warnings=[],
        )


class FakeDownloadTool:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, request, context):
        self.calls.append(request)
        batch = len(request.urls)
        return SimpleNamespace(
            items=[object()] * batch,
            artifacts=[
                DownloadedArtifact(
                    path=f"/tmp/video-{len(self.calls)}-{index}.mp4",
                    size_bytes=1024,
                    sha256=f"{index + 1:064x}"[-64:],
                    media_type="video/mp4",
                )
                for index in range(batch)
            ],
            output_directory=f"/tmp/batch-{len(self.calls)}",
        )


class FakeWatermarkTool:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, request, context):
        self.calls.append(request)
        return SimpleNamespace(
            detected_count=len(request.artifacts),
            processed_count=len(request.artifacts),
            output_directory=f"/tmp/watermark-{len(self.calls)}",
            items=[],
        )


def _plan(limit: int = 100) -> AgentPlan:
    return AgentPlan(
        objective="搜索并下载",
        platform="douyin",
        session_ref=SESSION_REF,
        source="search",
        view="top",
        query="web3",
        limit=limit,
        download=True,
        download_batch_size=20,
        max_scrolls=30,
        tool_call_budget=1 + (limit + 19) // 20,
    )


def test_runtime_browses_once_and_downloads_in_twenty_url_batches() -> None:
    browse = FakeBrowseTool(100)
    download = FakeDownloadTool()
    agent = SocialOperationsAgent(browse_tool=browse, download_tool=download)

    result = asyncio.run(agent.execute_plan(_plan()))

    assert len(browse.calls) == 1
    assert browse.calls[0].max_items == 100
    assert [len(call.urls) for call in download.calls] == [20, 20, 20, 20, 20]
    assert result.downloaded_items == 100
    assert result.artifact_count == 100
    assert result.tool_calls_used == 6


def test_runtime_reports_when_page_has_fewer_posts() -> None:
    agent = SocialOperationsAgent(
        browse_tool=FakeBrowseTool(12),
        download_tool=FakeDownloadTool(),
    )

    result = asyncio.run(agent.execute_plan(_plan(20)))

    assert len(result.discovered_urls) == 12
    assert result.downloaded_items == 12
    assert any("实际发现 12" in warning for warning in result.warnings)


def test_runtime_invokes_watermark_tool_after_each_download_batch() -> None:
    browse = FakeBrowseTool(20)
    download = FakeDownloadTool()
    watermark = FakeWatermarkTool()
    agent = SocialOperationsAgent(
        browse_tool=browse,
        download_tool=download,
        watermark_tool=watermark,
    )
    plan = _plan(20).model_copy(
        update={"remove_watermark": True, "tool_call_budget": 3}
    )

    result = asyncio.run(
        agent.execute_plan(plan, authorization_confirmed=True)
    )

    assert len(watermark.calls) == 1
    assert watermark.calls[0].authorization_confirmed is True
    assert result.watermark_detected_count == 20
    assert result.watermark_processed_count == 20
    assert result.tool_calls_used == 3
