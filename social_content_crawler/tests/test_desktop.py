from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog

from social_content_crawler.desktop import (
    DEFAULT_ALLOWED_DOMAINS,
    MainWindow,
    RegisteredSessionsDialog,
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
    assert window.manage_sessions_button.text() == "管理浏览器窗口"
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

    assert dialog.windowTitle() == "注册新窗口"
    assert not hasattr(dialog, "registered_combo")
    assert not hasattr(dialog, "manage_registered_button")
    assert [
        dialog.platform_combo.itemData(index)
        for index in range(dialog.platform_combo.count())
    ] == ["douyin", "xiaohongshu", "telegram", "x"]
    assert dialog.platform_combo.itemText(0) == SESSION_PLATFORM_LABELS["douyin"]
    assert dialog.platform_combo.itemText(1) == SESSION_PLATFORM_LABELS["xiaohongshu"]
    dialog.close()
    app.processEvents()


def test_existing_sessions_are_managed_in_a_separate_dialog(tmp_path: Path) -> None:
    from social_content_crawler.sessions import SessionRegistry

    app = QApplication.instance() or QApplication([])
    dialog = RegisteredSessionsDialog(SessionRegistry(tmp_path / "sessions.json"))

    assert dialog.windowTitle() == "管理浏览器窗口"
    assert dialog.registered_list.count() == 0
    assert not dialog.empty_label.isHidden()
    assert dialog.register_new_button.text() == "注册新窗口"
    assert not dialog.remove_button.isEnabled()
    dialog.close()
    app.processEvents()


def test_management_window_reports_readiness_only_after_exposure(tmp_path, monkeypatch) -> None:
    from PySide6.QtTest import QTest
    from social_content_crawler.sessions import SessionRegistry

    ready_file = tmp_path / "ready"
    monkeypatch.setenv("SOCIAL_AGENT_GUI_READY_FILE", str(ready_file))
    app = QApplication.instance() or QApplication([])
    dialog = RegisteredSessionsDialog(SessionRegistry(tmp_path / "sessions.json"))
    dialog._report_window_ready()
    assert not ready_file.exists()

    dialog.show()
    QTest.qWait(150)
    assert ready_file.read_text(encoding="utf-8") == str(os.getpid())
    assert not dialog._ready_timer.isActive()
    dialog.close()
    app.processEvents()


def test_nested_registration_does_not_acknowledge_management_readiness(tmp_path, monkeypatch) -> None:
    from PySide6.QtTest import QTest
    from social_content_crawler.sessions import SessionRegistry

    ready_file = tmp_path / "ready"
    monkeypatch.setenv("SOCIAL_AGENT_GUI_READY_FILE", str(ready_file))
    app = QApplication.instance() or QApplication([])
    registry = SessionRegistry(tmp_path / "sessions.json")
    manager = RegisteredSessionsDialog(registry)
    registration = SessionManagerDialog(registry, manager)
    registration.show()
    QTest.qWait(100)
    assert registration._ready_path is None
    assert not ready_file.exists()
    registration.close()
    manager.close()
    app.processEvents()


@pytest.fixture
def registration_form(tmp_path, monkeypatch):
    from social_content_crawler.sessions import SessionRegistry

    calls = []

    class FakeClient:
        def __init__(self, api_url):
            self.api_url = api_url

        def profile_detail(self, profile_id):
            calls.append((self.api_url, profile_id))
            return {"name": "测试 X 窗口"}

    monkeypatch.setattr("social_content_crawler.sessions._validate_profile_login", lambda *_: None)
    registry = SessionRegistry(tmp_path / "sessions.json", client_factory=FakeClient)
    app = QApplication.instance() or QApplication([])
    dialog = SessionManagerDialog(registry)
    dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData("x"))
    dialog.api_url_input.setText("http://127.0.0.1:54345")
    dialog.profile_combo.clear()
    dialog.profile_combo.addItem("测试 X 窗口", "profile-x")
    dialog.show()
    app.processEvents()
    yield dialog, registry, calls
    dialog.close()
    app.processEvents()


@pytest.mark.parametrize("save", [True, False])
def test_management_opens_registration_and_refreshes_list(registration_form, monkeypatch, save) -> None:
    from PySide6.QtCore import Qt

    _, registry, calls = registration_form
    manager = RegisteredSessionsDialog(registry)
    changes = []
    opened = []
    manager.sessions_changed.connect(lambda: changes.append(True))

    def register_or_cancel(dialog):
        opened.append(dialog.windowTitle())
        assert dialog.parent() is manager
        assert not hasattr(dialog, "manage_registered_button")
        dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData("x"))
        dialog.api_url_input.setText("http://127.0.0.1:54345")
        dialog.profile_combo.clear()
        dialog.profile_combo.addItem("测试 X 窗口", "profile-x")
        if save:
            dialog.finish_button.click()
            # The manager is updated by the registration signal, before exec returns.
            assert manager.registered_list.count() == 1
        else:
            dialog.cancel_button.click()
        return dialog.result()

    monkeypatch.setattr(SessionManagerDialog, "exec", register_or_cancel)
    manager.register_new_button.click()

    assert opened == ["注册新窗口"]
    assert len(registry.list()) == len(calls) == len(changes) == int(save)
    assert manager.registered_list.count() == int(save)
    assert manager.remove_button.isEnabled() == save
    if save:
        item = manager.registered_list.currentItem()
        assert item.data(Qt.ItemDataRole.UserRole) == registry.list()[0].session_ref
        assert "X / Twitter · 测试 X 窗口" in item.text()
        assert manager.empty_label.isHidden()
        manager.remove_button.click()
        assert registry.list() == []
        assert manager.registered_list.count() == 0
        assert not manager.remove_button.isEnabled()
        assert not manager.empty_label.isHidden()
        assert len(changes) == 2
    manager.close()


def test_download_gui_opens_management_before_registration(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    opened = []
    monkeypatch.setattr(RegisteredSessionsDialog, "exec", lambda dialog: opened.append(dialog.windowTitle()))
    window = MainWindow(output_root=tmp_path, session_registry_path=tmp_path / "sessions.json")
    window.manage_sessions_button.click()
    assert opened == ["管理浏览器窗口"]
    window.close()
    app.processEvents()


def test_finish_registers_selected_profile_before_closing(registration_form) -> None:
    dialog, registry, calls = registration_form
    changes = []
    dialog.sessions_changed.connect(lambda: changes.append(True))
    assert dialog.finish_button.text() == "注册并完成"

    dialog.finish_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert not dialog.isVisible()
    assert len(registry.list()) == 1
    assert registry.list()[0].platform == "x"
    assert registry.list()[0].profile_id == "profile-x"
    assert len(calls) == len(changes) == 1


def test_generate_then_finish_reuses_the_registered_reference(registration_form) -> None:
    dialog, registry, calls = registration_form
    dialog.register_button.click()
    reference = registry.list()[0].session_ref
    assert dialog.isVisible()
    assert dialog.finish_button.text() == "完成"

    dialog.finish_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert registry.list()[0].session_ref == reference
    assert len(calls) == 1


@pytest.mark.parametrize("changed_field", ["platform", "api", "profile"])
def test_changed_selection_must_be_registered_again(registration_form, changed_field) -> None:
    dialog, registry, calls = registration_form
    dialog.register_button.click()
    previous_ref = dialog.session_ref_output.text()
    if changed_field == "platform":
        dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData("douyin"))
    elif changed_field == "api":
        dialog.api_url_input.setText("http://127.0.0.1:54346")
    else:
        dialog.profile_combo.addItem("第二窗口", "profile-other")
        dialog.profile_combo.setCurrentIndex(1)
    assert dialog.finish_button.text() == "注册并完成"
    assert not dialog.session_ref_output.text()

    dialog.finish_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert len(calls) == 2
    assert len(registry.list()) == 2
    assert dialog.session_ref_output.text() != previous_ref


def test_failed_registration_keeps_dialog_open(registration_form, monkeypatch) -> None:
    from social_content_crawler.errors import CrawlerError, ErrorCode

    dialog, registry, _ = registration_form
    warnings = []

    def fail_login(*_args):
        raise CrawlerError(ErrorCode.SESSION_REAUTH_REQUIRED, "请先登录 X")

    monkeypatch.setattr("social_content_crawler.sessions._validate_profile_login", fail_login)
    monkeypatch.setattr("social_content_crawler.desktop.QMessageBox.warning", lambda *args: warnings.append(args))
    dialog.finish_button.click()

    assert dialog.isVisible()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert registry.list() == []
    assert warnings[-1][-1] == "请先登录 X"
    assert dialog.finish_button.text() == "注册并完成"


def test_finish_without_profile_stays_open(registration_form, monkeypatch) -> None:
    dialog, registry, calls = registration_form
    warnings = []
    monkeypatch.setattr("social_content_crawler.desktop.QMessageBox.warning", lambda *args: warnings.append(args))
    dialog.profile_combo.clear()
    dialog.finish_button.click()
    assert dialog.isVisible()
    assert warnings
    assert calls == []
    assert registry.list() == []


def test_cancel_does_not_register_profile(registration_form) -> None:
    dialog, registry, calls = registration_form
    dialog.cancel_button.click()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert not dialog.isVisible()
    assert registry.list() == []
    assert calls == []


def test_revoked_reference_is_not_reused_by_finish(registration_form) -> None:
    dialog, registry, calls = registration_form
    dialog.register_button.click()
    previous_ref = dialog.session_ref_output.text()
    registry.revoke(previous_ref)
    dialog.finish_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert registry.list()[0].session_ref != previous_ref
    assert len(calls) == 2


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
