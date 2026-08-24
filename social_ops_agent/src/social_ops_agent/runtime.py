from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from social_content_crawler import (
    BrowsePostsInput,
    DownloadInput,
    InMemoryAuditSink,
    LocalRateLimiter,
    PublicHttpsUrlPolicy,
    SessionRegistry,
    SocialMediaDownloadTool,
    SocialPostBrowseTool,
    SocialPostBrowserBackend,
    YtDlpBackend,
)
from social_content_crawler.platforms import default_allowed_domains
from social_content_crawler.ports import ToolContext

from .contracts import AgentPlan, AgentProgress, AgentRunResult


class ExecutableTool(Protocol):
    async def execute(self, request: Any, context: ToolContext) -> Any: ...


ProgressCallback = Callable[[AgentProgress], None]
CancelCheck = Callable[[], bool]


class SocialOperationsAgent:
    """Finite-state local agent that composes browse and download Tools."""

    def __init__(
        self,
        *,
        browse_tool: ExecutableTool,
        download_tool: ExecutableTool,
        watermark_tool: ExecutableTool | None = None,
    ) -> None:
        self._browse_tool = browse_tool
        self._download_tool = download_tool
        self._watermark_tool = watermark_tool

    @classmethod
    def local(
        cls,
        *,
        session_registry: SessionRegistry,
        output_root: Path,
        download_progress: Callable[[dict[str, Any]], None] | None = None,
        state_root: Path | None = None,
    ) -> SocialOperationsAgent:
        audit = InMemoryAuditSink()
        limiter = LocalRateLimiter()
        browse_tool = SocialPostBrowseTool(
            backend=SocialPostBrowserBackend(session_registry=session_registry),
            audit_sink=audit,
            rate_limiter=limiter,
        )
        download_tool = SocialMediaDownloadTool(
            backend=YtDlpBackend(
                session_registry=session_registry,
                progress_callback=download_progress,
            ),
            audit_sink=audit,
            rate_limiter=limiter,
            url_policy=PublicHttpsUrlPolicy(),
            output_root=output_root,
            allowed_domains=default_allowed_domains(),
        )
        watermark_tool = None
        try:
            from media_content_analyzer import build_local_watermark_tool

            watermark_tool = build_local_watermark_tool(
                allowed_media_root=output_root,
                state_root=state_root or output_root / ".social-agent-state",
                output_root=output_root / "watermark-processed",
            )
        except ImportError:
            pass
        return cls(
            browse_tool=browse_tool,
            download_tool=download_tool,
            watermark_tool=watermark_tool,
        )

    async def execute_plan(
        self,
        plan: AgentPlan,
        *,
        progress: ProgressCallback | None = None,
        should_cancel: CancelCheck | None = None,
        authorization_confirmed: bool = False,
    ) -> AgentRunResult:
        notify = progress or (lambda event: None)
        cancelled = should_cancel or (lambda: False)
        if plan.remove_watermark and not authorization_confirmed:
            raise ValueError("watermark removal requires confirmation from the user")
        if plan.remove_watermark and self._watermark_tool is None:
            raise RuntimeError("media.process_watermark is not installed")
        context = ToolContext(
            tenant_id="local-agent",
            trace_id=f"social-agent-{uuid.uuid4().hex}",
            agent_run_id=uuid.uuid4().hex,
            actor_type="agent",
            actor_id="social-ops-agent",
        )
        notify(AgentProgress(stage="browse", completed=0, total=plan.limit, message="正在浏览并提取帖子 URL…"))
        browse_output = await self._browse_tool.execute(
            BrowsePostsInput(
                platform=plan.platform.value,
                session_ref=plan.session_ref,
                source=plan.source.value,
                view=plan.view.value,
                query=plan.query,
                user_key=plan.user_key,
                start_url=plan.start_url,
                max_items=plan.limit,
                max_scrolls=plan.max_scrolls,
            ),
            context,
        )
        urls = [post.url for post in browse_output.posts][: plan.limit]
        warnings = list(browse_output.warnings)
        if len(urls) < plan.limit:
            warnings.append(f"目标为 {plan.limit} 条，页面实际发现 {len(urls)} 条可识别帖子。")
        notify(
            AgentProgress(
                stage="browse",
                completed=len(urls),
                total=max(plan.limit, 1),
                message=f"已发现 {len(urls)} 个帖子 URL。",
            )
        )
        tool_calls = 1
        if not plan.download or cancelled():
            return AgentRunResult(
                plan=plan,
                discovered_urls=urls,
                downloaded_items=0,
                artifact_count=0,
                tool_calls_used=tool_calls,
                cancelled=cancelled(),
                warnings=warnings,
            )

        downloaded_items = 0
        artifact_count = 0
        watermark_detected_count = 0
        watermark_processed_count = 0
        downloaded_bytes = 0
        maximum_bytes = plan.max_total_download_mb * 1024 * 1024
        output_directories: list[str] = []
        watermark_output_directories: list[str] = []
        for offset in range(0, len(urls), plan.download_batch_size):
            if cancelled():
                break
            batch = urls[offset : offset + plan.download_batch_size]
            notify(
                AgentProgress(
                    stage="download",
                    completed=downloaded_items,
                    total=max(len(urls), 1),
                    message=f"正在下载第 {offset + 1}–{offset + len(batch)} 条…",
                )
            )
            output = await self._download_tool.execute(
                DownloadInput(
                    urls=batch,
                    media_format=plan.media_format.value,
                    max_items=len(batch),
                    max_total_size_mb=max(
                        1,
                        min(5_000, (maximum_bytes - downloaded_bytes) // (1024 * 1024)),
                    ),
                    session_ref=plan.session_ref,
                ),
                context,
            )
            tool_calls += 1
            downloaded_items += len(output.items)
            artifact_count += len(output.artifacts)
            downloaded_bytes += sum(
                int(getattr(artifact, "size_bytes", 0)) for artifact in output.artifacts
            )
            if output.output_directory:
                output_directories.append(output.output_directory)
            if plan.remove_watermark and output.artifacts:
                from media_content_analyzer import ArtifactRef, ProcessWatermarkInput

                video_artifacts = [
                    ArtifactRef.model_validate(artifact.model_dump())
                    for artifact in output.artifacts
                    if (artifact.media_type or "").startswith("video/")
                ]
                if video_artifacts:
                    notify(
                        AgentProgress(
                            stage="watermark",
                            completed=offset,
                            total=max(len(urls), 1),
                            message=f"正在检查第 {offset + 1}–{offset + len(batch)} 条视频的水印…",
                        )
                    )
                    watermark_output = await self._watermark_tool.execute(
                        ProcessWatermarkInput(
                            artifacts=video_artifacts,
                            mode="remove_if_present",
                            authorization_confirmed=True,
                            minimum_confidence=plan.watermark_minimum_confidence,
                        ),
                        context,
                    )
                    tool_calls += 1
                    watermark_detected_count += watermark_output.detected_count
                    watermark_processed_count += watermark_output.processed_count
                    if watermark_output.output_directory:
                        watermark_output_directories.append(watermark_output.output_directory)
                    for item in watermark_output.items:
                        warnings.extend(item.warnings)
            notify(
                AgentProgress(
                    stage="download",
                    completed=min(offset + len(batch), len(urls)),
                    total=max(len(urls), 1),
                    message=f"已完成 {min(offset + len(batch), len(urls))}/{len(urls)} 条。",
                )
            )
            await asyncio.sleep(0)
            if downloaded_bytes >= maximum_bytes:
                warnings.append(
                    f"已达到计划的 {plan.max_total_download_mb} MB 总下载预算，停止后续批次。"
                )
                break

        return AgentRunResult(
            plan=plan,
            discovered_urls=urls,
            downloaded_items=downloaded_items,
            artifact_count=artifact_count,
            watermark_detected_count=watermark_detected_count,
            watermark_processed_count=watermark_processed_count,
            output_directories=output_directories,
            watermark_output_directories=watermark_output_directories,
            tool_calls_used=tool_calls,
            cancelled=cancelled(),
            warnings=warnings,
        )
