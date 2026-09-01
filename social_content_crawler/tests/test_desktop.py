from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from social_content_crawler.desktop import (
    DEFAULT_ALLOWED_DOMAINS,
    MainWindow,
    SESSION_PLATFORM_LABELS,
    SessionManagerDialog,
    extract_post_url,
    format_bytes,
    format_duration,
    is_telegram_channel_url,
)
from social_content_crawler.platforms import PLATFORM_CATALOG, supported_platform_label


def test_desktop_window_has_download_controls(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        output_root=tmp_path,
        allowed_domains=frozenset({"example.com"}),
        session_registry_path=tmp_path / "sessions.json",
    )

    assert "社媒帖子下载器" in window.windowTitle()
    assert window.url_input.placeholderText().startswith("https://")
    assert window.browser_session_check.isChecked()
    assert window.session_combo.itemText(0) == "不使用登录会话（公开帖子）"
    assert window.manage_sessions_button.text() == "管理登录会话"
    assert [window.format_combo.itemText(index) for index in range(3)] == [
        "音视频",
        "仅视频",
        "仅音频",
    ]
    assert window.download_button.text().startswith("开始下载")
    assert window.result_card.isHidden()
    window.close()
    app.processEvents()


def test_session_manager_lists_mainland_platforms_first(tmp_path: Path) -> None:
    from social_content_crawler.sessions import SessionRegistry

    app = QApplication.instance() or QApplication([])
    dialog = SessionManagerDialog(SessionRegistry(tmp_path / "sessions.json"))

    assert dialog.windowTitle() == "管理社媒登录会话"
    assert [
        dialog.platform_combo.itemData(index)
        for index in range(dialog.platform_combo.count())
    ] == ["douyin", "xiaohongshu", "telegram", "x"]
    assert dialog.platform_combo.itemText(0) == SESSION_PLATFORM_LABELS["douyin"]
    assert dialog.platform_combo.itemText(1) == SESSION_PLATFORM_LABELS["xiaohongshu"]
    dialog.close()
    app.processEvents()


def test_desktop_formatters() -> None:
    assert format_bytes(1_048_576) == "1.0 MB"
    assert format_duration(125) == "2:05"


def test_mainland_china_platforms_are_enabled() -> None:
    assert "douyin.com" in DEFAULT_ALLOWED_DOMAINS
    assert "xiaohongshu.com" in DEFAULT_ALLOWED_DOMAINS
    assert "xhslink.com" in DEFAULT_ALLOWED_DOMAINS
    assert "xhslink.cn" in DEFAULT_ALLOWED_DOMAINS
    assert "t.me" in DEFAULT_ALLOWED_DOMAINS


def test_platform_catalog_drives_domain_allowlist_and_ui() -> None:
    assert len(PLATFORM_CATALOG) == 14
    assert DEFAULT_ALLOWED_DOMAINS == frozenset(
        domain for platform in PLATFORM_CATALOG for domain in platform.domains
    )
    label = supported_platform_label()
    assert all(platform.display_name in label for platform in PLATFORM_CATALOG)
    assert len({platform.key for platform in PLATFORM_CATALOG}) == len(PLATFORM_CATALOG)


def test_extracts_urls_from_chinese_share_text() -> None:
    assert extract_post_url("复制打开抖音 https://v.douyin.com/ABC123/ 看视频") == (
        "https://v.douyin.com/ABC123/"
    )
    assert extract_post_url("打开小红书查看 http://xhslink.com/m/ABC123 ，复制本条信息") == (
        "https://xhslink.com/m/ABC123"
    )
    assert extract_post_url("打开小红书查看 http://xhslink.cn/o/ABC123") == (
        "https://xhslink.cn/o/ABC123"
    )


def test_detects_telegram_channel_but_not_message_url() -> None:
    assert is_telegram_channel_url("https://t.me/weme_download")
    assert is_telegram_channel_url("https://t.me/c/1634371164")
    assert not is_telegram_channel_url("https://t.me/weme_download/123")
    assert not is_telegram_channel_url("https://t.me/c/1634371164/456")
