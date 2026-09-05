import pytest


pytest.importorskip("mcp")

from social_content_crawler.mcp_server import mcp


def test_plugin_mcp_exposes_crawler_tools() -> None:
    assert set(mcp._tool_manager._tools) == {
        "browse_posts",
        "discover_public_materials",
        "download_public_material",
        "browser_operate",
        "download_media",
        "publish_x_post",
    }
    download_schema = mcp._tool_manager._tools["download_media"].parameters
    assert download_schema["properties"]["telegram_scope"]["default"] == "messages"
    assert download_schema["properties"]["telegram_max_messages"]["default"] == 2000
