import pytest


pytest.importorskip("mcp")

from social_content_crawler.mcp_server import mcp


def test_plugin_mcp_exposes_crawler_tools() -> None:
    assert set(mcp._tool_manager._tools) == {
        "browse_posts",
        "browser_operate",
        "download_media",
        "publish_x_post",
    }
