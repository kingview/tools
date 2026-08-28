import pytest


pytest.importorskip("mcp")

from media_content_analyzer.mcp_server import mcp


def test_plugin_mcp_exposes_media_tools() -> None:
    assert set(mcp._tool_manager._tools) == {
        "analyze_content",
        "process_watermark",
        "generate_post_copy",
    }
