from __future__ import annotations

from .diagnostics import current_context, install_exception_hooks
from .diagnostic_mcp import DiagnosticFastMCP

import hashlib
import json
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any


from .contracts import (
    AnalyzeContentInput,
    ArtifactRef,
    ContentAnalysisOutput,
    CopyTone,
    GeneratePostCopyInput,
    ProcessWatermarkInput,
)
from .ports import ToolContext
from .runtime import build_local_copy_tool, build_local_tool
from .watermark_runtime import build_local_watermark_tool


mcp = DiagnosticFastMCP(
    "media-content",
    diagnostic_component="media-content",
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
        diagnostics = current_context()
        run_id = diagnostics.get("tool_call_id") or uuid.uuid4().hex
        return ToolContext(
            tenant_id="local-agent",
            trace_id=diagnostics.get("trace_id") or f"plugin-{run_id}",
            actor_type="agent",
            actor_id="media-content-plugin",
            agent_run_id=diagnostics.get("execution_id") or run_id,
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
    normalized_tone, tone_instruction = _normalize_copy_tone(tone)
    instructions = extra_instructions
    if tone_instruction:
        instructions = "\n".join(
            filter(None, [tone_instruction, extra_instructions])
        )[:10_000]
    request = GeneratePostCopyInput(
        analysis=_coerce_content_analysis(analysis),
        platform=platform,
        tone=normalized_tone,
        objective=objective,
        extra_instructions=instructions,
        variant_count=variant_count,
        max_characters=max_characters,
    )
    result = await tool_runtime.copy.execute(request, tool_runtime.context())
    return result.model_dump(mode="json")


def _coerce_content_analysis(raw: dict[str, Any]) -> ContentAnalysisOutput:
    """Accept both analyzer output and compact Agent grounding summaries.

    Harness can call copy generation directly after browsing, before a local
    media artifact exists. In that case it naturally supplies a concise dict
    instead of the analyzer's full contract. Normalize that shorthand here so
    the generation Tool remains composable without asking the model to invent
    implementation-only fields.
    """
    try:
        return ContentAnalysisOutput.model_validate(raw)
    except (TypeError, ValueError):
        pass

    confidence = _confidence(raw.get("confidence"), default=0.5)
    topics = _string_list(raw.get("topics"))
    tags = _normalized_tags(raw.get("tags"), confidence)
    if not topics:
        topics = [item["label"] for item in tags if item["namespace"] == "topic"]
    summary = str(raw.get("summary") or "").strip()
    if not summary:
        summary = _summary_from_grounding(raw)
    return ContentAnalysisOutput.model_validate(
        {
            "language": str(raw.get("language") or "zh"),
            "summary": summary,
            "tags": tags,
            "topics": topics,
            "entities": _string_list(raw.get("entities")),
            "claims": _string_list(raw.get("claims")),
            "image_summary": _optional_text(raw.get("image_summary")),
            "video_summary": _optional_text(raw.get("video_summary")),
            "transcript_summary": _optional_text(raw.get("transcript_summary")),
            "sentiment": str(raw.get("sentiment") or "neutral"),
            "commercial_intent": _optional_text(raw.get("commercial_intent")),
            "safety_flags": _string_list(raw.get("safety_flags")),
            "confidence": confidence,
            "evidence": _normalized_evidence(raw.get("evidence"), confidence),
            "needs_human_review": bool(raw.get("needs_human_review", True)),
            "assets": [],
            "warnings": _string_list(raw.get("warnings")),
            "cache_hit": bool(raw.get("cache_hit", False)),
            "pipeline_version": str(raw.get("pipeline_version") or "agent-grounding-v1"),
            "model_versions": {
                str(key): str(value)
                for key, value in (raw.get("model_versions") or {}).items()
            }
            if isinstance(raw.get("model_versions"), dict)
            else {},
        }
    )


def _normalize_copy_tone(value: str) -> tuple[CopyTone, str | None]:
    candidate = str(value or "natural").strip()
    try:
        return CopyTone(candidate), None
    except ValueError:
        lowered = candidate.lower()
        mappings = (
            (("暧昧", "擦边", "suggest"), CopyTone.SUGGESTIVE),
            (("推荐", "种草", "recommend"), CopyTone.RECOMMENDATION),
            (("专业", "professional"), CopyTone.PROFESSIONAL),
            (("幽默", "搞笑", "humor"), CopyTone.HUMOROUS),
            (("情绪", "情感", "emotional"), CopyTone.EMOTIONAL),
        )
        normalized = next(
            (tone for markers, tone in mappings if any(marker in lowered for marker in markers)),
            CopyTone.NATURAL,
        )
        return normalized, f"用户期望的文案语气：{candidate}"


def _normalized_tags(value: Any, default_confidence: float) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    allowed = {"topic", "entity", "object", "format", "sentiment", "commercial", "safety"}
    result = []
    for item in value:
        if isinstance(item, dict):
            label = _optional_text(item.get("label") or item.get("name") or item.get("value"))
            namespace = str(item.get("namespace") or "topic").lower()
            evidence_refs = _string_list(item.get("evidence_refs"))
            tag_confidence = _confidence(item.get("confidence"), default=default_confidence)
        else:
            label = _optional_text(item)
            namespace = "topic"
            evidence_refs = []
            tag_confidence = default_confidence
        if label:
            result.append(
                {
                    "namespace": namespace if namespace in allowed else "topic",
                    "label": label[:200],
                    "confidence": tag_confidence,
                    "evidence_refs": evidence_refs[:50],
                }
            )
    return result


def _normalized_evidence(value: Any, default_confidence: float) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    allowed = {"post_text", "ocr", "transcript", "visual", "metadata"}
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            text = _optional_text(item)
            item = {"text": text} if text else {}
        text = _optional_text(item.get("text"))
        raw_kind = str(item.get("kind") or item.get("type") or "metadata").lower()
        if raw_kind not in allowed:
            raw_kind = "post_text" if "text" in raw_kind else "metadata"
        result.append(
            {
                "evidence_id": str(item.get("evidence_id") or f"agent-evidence-{index}"),
                "kind": raw_kind,
                "text": text[:10_000] if text else None,
                "confidence": _confidence(item.get("confidence"), default=default_confidence),
            }
        )
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("name") or item.get("label") or item.get("text")
        text = _optional_text(item)
        if text:
            result.append(text)
    return result


def _summary_from_grounding(raw: dict[str, Any]) -> str:
    source = _optional_text(raw.get("source"))
    query = _optional_text(raw.get("query"))
    selected = raw.get("selected_posts_attempted")
    fragments = [value for value in (source, query) if value]
    if isinstance(selected, list):
        fragments.extend(
            text
            for item in selected
            if isinstance(item, dict)
            for text in [_optional_text(item.get("text"))]
            if text
        )
    if fragments:
        return "；".join(fragments)[:20_000]
    return json.dumps(raw, ensure_ascii=False, default=str)[:20_000] or "暂无可用内容摘要"


def _confidence(value: Any, *, default: float) -> float:
    labels = {"高": 0.85, "中": 0.6, "低": 0.35, "high": 0.85, "medium": 0.6, "low": 0.35}
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = labels.get(str(value).strip().lower(), default)
    return min(1.0, max(0.0, number))


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_path(name: str) -> Path:
    raw = os.getenv(name, "").strip()
    if not raw:
        raise RuntimeError(f"{name} is required")
    path = Path(raw).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> None:
    install_exception_hooks("media-content")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
