from __future__ import annotations

import hashlib
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .contracts import (
    AnalyzeContentInput,
    ArtifactRef,
    ContentAnalysisOutput,
    GeneratePostCopyInput,
    ProcessWatermarkInput,
)
from .ports import ToolContext
from .runtime import build_local_copy_tool, build_local_tool
from .watermark_runtime import build_local_watermark_tool


mcp = FastMCP(
    "media-content",
    instructions="Analyze local media and create local derived files; never publish content.",
)


class Runtime:
    def __init__(self) -> None:
        self.output_root = _required_path("SOCIAL_AGENT_OUTPUT_ROOT")
        self.state_root = _required_path("SOCIAL_AGENT_STATE_ROOT")
        model_base_url = os.getenv("SOCIAL_AGENT_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
        model_name = os.getenv("SOCIAL_AGENT_LLM_MODEL", "qwen3.5:9b")
        model_api_key = os.getenv("SOCIAL_AGENT_LLM_API_KEY", "local-model")
        self.analyze = build_local_tool(
            allowed_media_root=self.output_root,
            state_root=self.state_root / "analysis",
            model_base_url=model_base_url,
            model_name=model_name,
            model_api_key=model_api_key,
        )
        self.copy = build_local_copy_tool(
            state_root=self.state_root / "copy",
            model_base_url=model_base_url,
            model_name=model_name,
            model_api_key=model_api_key,
        )
        self.watermark = build_local_watermark_tool(
            allowed_media_root=self.output_root,
            state_root=self.state_root / "watermark",
            output_root=self.output_root / "watermark-processed",
        )

    @staticmethod
    def context() -> ToolContext:
        run_id = uuid.uuid4().hex
        return ToolContext(
            tenant_id="local-agent",
            trace_id=f"plugin-{run_id}",
            actor_type="agent",
            actor_id="media-content-plugin",
            agent_run_id=run_id,
        )

    def artifact(self, raw_path: str) -> ArtifactRef:
        path = Path(raw_path).expanduser().resolve(strict=True)
        root = self.output_root.resolve()
        if path != root and root not in path.parents:
            raise ValueError("media path is outside the configured Social Agent output directory")
        if not path.is_file():
            raise ValueError("media path is not a file")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return ArtifactRef(
            path=str(path),
            size_bytes=path.stat().st_size,
            sha256=digest.hexdigest(),
            media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )


_runtime: Runtime | None = None


def runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime()
    return _runtime


@mcp.tool()
async def analyze_content(
    file_paths: list[str],
    post_text: str | None = None,
    source_url: str | None = None,
    language_hint: str | None = "zh",
) -> dict[str, Any]:
    tool_runtime = runtime()
    request = AnalyzeContentInput(
        artifacts=[tool_runtime.artifact(path) for path in file_paths],
        post_text=post_text,
        source_url=source_url,
        language_hint=language_hint,
    )
    result = await tool_runtime.analyze.execute(request, tool_runtime.context())
    return result.model_dump(mode="json")


@mcp.tool()
async def process_watermark(
    file_paths: list[str],
    minimum_confidence: float = 0.72,
    repair_quality: str = "auto",
) -> dict[str, Any]:
    tool_runtime = runtime()
    request = ProcessWatermarkInput(
        artifacts=[tool_runtime.artifact(path) for path in file_paths],
        mode="remove_if_present",
        authorization_confirmed=True,
        minimum_confidence=minimum_confidence,
        repair_quality=repair_quality,
    )
    result = await tool_runtime.watermark.execute(request, tool_runtime.context())
    return result.model_dump(mode="json")


@mcp.tool()
async def generate_post_copy(
    analysis: dict[str, Any],
    platform: str = "generic",
    tone: str = "natural",
    objective: str | None = None,
    extra_instructions: str | None = None,
    variant_count: int = 3,
    max_characters: int = 300,
) -> dict[str, Any]:
    tool_runtime = runtime()
    request = GeneratePostCopyInput(
        analysis=ContentAnalysisOutput.model_validate(analysis),
        platform=platform,
        tone=tone,
        objective=objective,
        extra_instructions=extra_instructions,
        variant_count=variant_count,
        max_characters=max_characters,
    )
    result = await tool_runtime.copy.execute(request, tool_runtime.context())
    return result.model_dump(mode="json")


def _required_path(name: str) -> Path:
    raw = os.getenv(name, "").strip()
    if not raw:
        raise RuntimeError(f"{name} is required")
    path = Path(raw).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
