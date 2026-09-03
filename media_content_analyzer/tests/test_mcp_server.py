import pytest


pytest.importorskip("mcp")

from media_content_analyzer.mcp_server import (
    _coerce_content_analysis,
    _normalize_copy_tone,
    mcp,
)


def test_plugin_mcp_exposes_media_tools() -> None:
    assert set(mcp._tool_manager._tools) == {
        "analyze_content",
        "process_watermark",
        "generate_post_copy",
    }


def test_mcp_wrapper_logs_failures_before_tool_is_constructed(tmp_path, monkeypatch, capsys):
    import asyncio
    import json
    from media_content_analyzer import mcp_server
    monkeypatch.setenv("SOCIAL_AGENT_LOG_DIR", str(tmp_path / "logs"))
    failure = RuntimeError("Cookie: session=PRIVATE-COOKIE")

    def unavailable():
        raise failure

    monkeypatch.setattr(mcp_server, "runtime", unavailable)
    result = asyncio.run(mcp_server.mcp.call_tool("analyze_content", {"file_paths": ["private-file-path"], "post_text": "private post"}))
    assert result.isError
    assert result.meta["com.socialagent/diagnostics"]["error_id"]
    content = next((tmp_path / "logs").glob("*.jsonl")).read_text()
    assert json.loads(content)["stage"] == "mcp.analyze_content"
    assert all(value not in content for value in ("PRIVATE-COOKIE", "private-file-path", "private post"))
    assert not capsys.readouterr().out


def test_copy_mcp_accepts_compact_agent_grounding() -> None:
    output = _coerce_content_analysis(
        {
            "source": "xiaohongshu_search_results",
            "query": "城市夜景",
            "selected_posts_attempted": [{"text": "今晚的灯很好看"}],
            "confidence": "高",
        }
    )

    assert output.language == "zh"
    assert "今晚的灯很好看" in output.summary
    assert output.confidence == 0.85


def test_copy_mcp_normalizes_common_llm_shorthand() -> None:
    output = _coerce_content_analysis(
        {
            "language": "zh",
            "summary": "小红书帖子风格归纳",
            "tags": ["小红书", {"name": "氛围感", "confidence": "中"}],
            "topics": ["文案"],
            "entities": [{"name": "小红书", "type": "platform"}],
            "claims": ["短句更容易阅读"],
            "sentiment": "positive",
            "safety_flags": [],
            "confidence": 0.62,
            "evidence": [
                {
                    "type": "search_result_text",
                    "text": "一些吃饭照",
                    "url": "https://www.xiaohongshu.com/explore/example",
                }
            ],
            "needs_human_review": True,
            "assets": [],
            "pipeline_version": "agent",
            "model_versions": {"analysis": "tool-summary"},
        }
    )

    assert [tag.label for tag in output.tags] == ["小红书", "氛围感"]
    assert output.entities == ["小红书"]
    assert output.evidence[0].kind == "post_text"


def test_copy_mcp_preserves_freeform_tone_as_instruction() -> None:
    tone, instruction = _normalize_copy_tone("暧昧吸引、自然小红书风")

    assert tone.value == "suggestive"
    assert instruction == "用户期望的文案语气：暧昧吸引、自然小红书风"
