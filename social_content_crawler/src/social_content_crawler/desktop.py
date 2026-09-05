from __future__ import annotations

from .diagnostics import install_exception_hooks, record_exception

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError
from PySide6.QtCore import QSize, QStandardPaths, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .backend import YtDlpBackend
from .contracts import BrowserCookieSource, DownloadInput, DownloadOutput, MediaFormat
from .errors import CrawlerError
from .platforms import default_allowed_domains, supported_platform_label
from .ports import ToolContext
from .runtime import InMemoryAuditSink, LocalRateLimiter
from .sessions import BitBrowserClient, SessionRegistry, default_session_registry_path
from .tool import SocialMediaDownloadTool
from .url_policy import PublicHttpsUrlPolicy


APP_NAME = "PostDrop"
DEFAULT_ALLOWED_DOMAINS = default_allowed_domains()
SESSION_PLATFORM_LABELS = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "telegram": "Telegram Web",
    "x": "X / Twitter",
}


class DownloadWorker(QThread):
    progress_changed = Signal(int, str)
    succeeded = Signal(object)
    failed = Signal(str, str)

    def __init__(
        self,
        request: DownloadInput,
        output_root: Path,
        allowed_domains: frozenset[str],
        session_registry_path: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._request = request
        self._output_root = output_root
        self._allowed_domains = allowed_domains
        self._session_registry_path = session_registry_path

    def run(self) -> None:
        backend = YtDlpBackend(
            progress_callback=self._on_progress,
            session_registry=SessionRegistry(self._session_registry_path),
        )
        tool = SocialMediaDownloadTool(
            backend=backend,
            audit_sink=InMemoryAuditSink(),
            rate_limiter=LocalRateLimiter(),
            url_policy=PublicHttpsUrlPolicy(),
            output_root=self._output_root,
            allowed_domains=self._allowed_domains,
        )
        try:
            output = asyncio.run(
                tool.execute(
                    self._request,
                    ToolContext(
                        tenant_id="local-desktop",
                        trace_id=f"desktop-{id(self)}",
                        actor_type="user",
                        actor_id="desktop-user",
                    ),
                )
            )
        except CrawlerError as exc:
            record_exception("social-content", "desktop.handled", exc)
            self.failed.emit(str(exc.code), str(exc))
        except Exception as exc:
            record_exception("social-content", "gui.download", exc)
            self.failed.emit(
                "unexpected_error",
                "下载失败。请确认帖子公开可访问、地址正确，并检查网络连接。",
            )
        else:
            self.progress_changed.emit(100, "下载完成")
            self.succeeded.emit(output)

    def _on_progress(self, event: dict[str, Any]) -> None:
        status = event.get("status")
        if status == "channel_scanning":
            completed = int(event.get("downloaded_messages") or 0)
            maximum = max(1, int(event.get("max_messages") or 1))
            percent = max(3, min(92, int(completed / maximum * 90)))
            self.progress_changed.emit(percent, f"正在遍历 Telegram 历史消息 · 已整理 {completed} 条")
            return
        if status == "finished":
            self.progress_changed.emit(96, "媒体已保存，正在整理文件…")
            return
        if status != "downloading":
            return
        downloaded = event.get("downloaded_bytes")
        total = event.get("total_bytes") or event.get("total_bytes_estimate")
        percent = int(downloaded / total * 92) if downloaded and total else 18
        percent = max(2, min(percent, 92))
        message = "正在下载媒体"
        speed = event.get("speed")
        eta = event.get("eta")
        details = []
        if isinstance(speed, (int, float)):
            details.append(f"{format_bytes(speed)}/s")
        if isinstance(eta, (int, float)):
            details.append(f"约 {int(eta)} 秒")
        if details:
            message = f"{message} · {' · '.join(details)}"
        self.progress_changed.emit(percent, message)


class _ReadyDialog(QDialog):
    """Acknowledge only the top-level plugin window once it is exposed."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("browserWindowDialog")
        raw_ready_path = os.getenv("SOCIAL_AGENT_GUI_READY_FILE", "").strip()
        self._ready_path = Path(raw_ready_path) if raw_ready_path and parent is None else None
        self._ready_timer = QTimer(self)
        self._ready_timer.setInterval(50)
        self._ready_timer.timeout.connect(self._report_window_ready)

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        if self._ready_path is not None:
            self._ready_timer.start()

    def _report_window_ready(self) -> None:
        handle = self.windowHandle()
        if not self.isVisible() or handle is None or not handle.isExposed():
            return
        self._ready_timer.stop()
        if self._ready_path is None:
            return
        try:
            temporary = self._ready_path.with_suffix(".tmp")
            temporary.write_text(str(os.getpid()), encoding="utf-8")
            temporary.replace(self._ready_path)
        except OSError as exc:
            record_exception("social-content", "desktop.handled", exc)
            print(f"Unable to acknowledge GUI readiness: {exc}", file=sys.stderr)
        self._ready_path = None


class RegisteredSessionsDialog(_ReadyDialog):
    sessions_changed = Signal()

    def __init__(self, registry: SessionRegistry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._registry = registry
        self.setWindowTitle("管理浏览器窗口")
        self.setMinimumWidth(680)
        self.resize(760, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        self.title_label = QLabel()
        self.title_label.setObjectName("dialogTitle")
        copy = QLabel("以下是已注册、可供 Agent 使用的浏览器窗口。移除引用不会删除比特浏览器窗口，也不会退出账号。")
        copy.setObjectName("dialogCopy")
        copy.setWordWrap(True)
        layout.addWidget(self.title_label)
        layout.addWidget(copy)

        rule = QLabel(
            "自动发现命名规则：平台-用途-编号，例如 DY-素材-01、XHS-美女-01、"
            "X-发布-01、TG-频道-01；也支持抖音、小红书、Twitter、Telegram 前缀。"
        )
        rule.setObjectName("dialogCopy")
        rule.setWordWrap(True)
        layout.addWidget(rule)
        auto_row = QHBoxLayout()
        self.api_url_input = QLineEdit()
        self.api_url_input.setObjectName("sessionInput")
        known_sessions = self._registry.list()
        self.api_url_input.setText(
            os.getenv(
                "BITBROWSER_API_URL",
                known_sessions[-1].api_url
                if known_sessions
                else "http://127.0.0.1:54345",
            )
        )
        self.api_url_input.setPlaceholderText("比特浏览器本地 API 地址")
        self.api_url_input.setFixedHeight(48)
        self.auto_register_button = QPushButton("自动发现并注册")
        self.auto_register_button.setObjectName("primaryButton")
        self.auto_register_button.clicked.connect(self.auto_register_windows)
        auto_row.addWidget(self.api_url_input, 1)
        auto_row.addWidget(self.auto_register_button)
        layout.addLayout(auto_row)

        self.registered_list = QListWidget()
        self.registered_list.setObjectName("registeredWindows")
        self.registered_list.setMinimumHeight(240)
        self.registered_list.setWordWrap(True)
        self.registered_list.currentItemChanged.connect(self._update_remove_button)
        layout.addWidget(self.registered_list, 1)
        self.empty_label = QLabel("尚未注册浏览器窗口，点击‘注册新窗口’添加。")
        self.empty_label.setObjectName("dialogCopy")
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label)

        self.register_new_button = QPushButton("注册新窗口")
        self.register_new_button.setObjectName("primaryButton")
        self.register_new_button.clicked.connect(self.register_new_window)
        self.remove_button = QPushButton("移除引用")
        self.remove_button.setObjectName("secondaryButton")
        self.remove_button.clicked.connect(self.remove_session)
        self._refresh_registered()

        close_button = QPushButton("完成")
        close_button.setObjectName("secondaryButton")
        close_button.clicked.connect(self.accept)
        for button in (
            self.auto_register_button,
            self.register_new_button,
            self.remove_button,
            close_button,
        ):
            button.setFixedHeight(48)
        close_row = QHBoxLayout()
        close_row.addWidget(self.register_new_button)
        close_row.addWidget(self.remove_button)
        close_row.addStretch()
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

    def remove_session(self) -> None:
        item = self.registered_list.currentItem()
        session_ref = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not session_ref:
            return
        try:
            self._registry.revoke(str(session_ref))
        except (CrawlerError, OSError) as exc:
            record_exception("social-content", "desktop.handled", exc)
            QMessageBox.warning(self, "无法移除引用", str(exc))
            return
        self._refresh_registered()
        self.sessions_changed.emit()

    def auto_register_windows(self) -> None:
        self.auto_register_button.setEnabled(False)
        self.auto_register_button.setText("正在发现…")
        QApplication.processEvents()
        try:
            report = self._registry.auto_register_named_profiles(
                self.api_url_input.text().strip()
            )
        except (CrawlerError, OSError) as exc:
            record_exception("social-content", "desktop.handled", exc)
            QMessageBox.warning(self, "自动发现没有完成", str(exc))
            return
        finally:
            self.auto_register_button.setEnabled(True)
            self.auto_register_button.setText("自动发现并注册")
        selected = report.registered[-1].session_ref if report.registered else None
        self._refresh_registered(selected)
        if report.registered:
            self.sessions_changed.emit()
        lines = [
            f"发现 {report.discovered} 个 Profile，新增注册 {len(report.registered)} 个，"
            f"已注册 {len(report.existing)} 个。"
        ]
        if report.unmatched:
            lines.append(f"{len(report.unmatched)} 个名称不符合规则，已跳过。")
        if report.errors:
            lines.append("未能注册：\n" + "\n".join(report.errors[:8]))
        QMessageBox.information(self, "自动发现完成", "\n\n".join(lines))

    def _refresh_registered(self, selected_ref: str | None = None) -> None:
        current = self.registered_list.currentItem()
        if selected_ref is None and current is not None:
            selected_ref = current.data(Qt.ItemDataRole.UserRole)
        self.registered_list.clear()
        records = self._registry.list()
        self.title_label.setText(f"已注册浏览器窗口（{len(records)}）")
        self.empty_label.setVisible(not records)
        for record in records:
            suffix = record.session_ref[-8:]
            platform_label = SESSION_PLATFORM_LABELS.get(record.platform, record.platform)
            item = QListWidgetItem(f"{platform_label} · {record.profile_name} · …{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, record.session_ref)
            item.setToolTip(f"Profile: {record.profile_id}\nsession_ref: {record.session_ref}")
            item.setSizeHint(QSize(0, 56))
            self.registered_list.addItem(item)
            if record.session_ref == selected_ref:
                self.registered_list.setCurrentItem(item)
        if records and self.registered_list.currentItem() is None:
            self.registered_list.setCurrentRow(0)
        self._update_remove_button()

    def _update_remove_button(self) -> None:
        self.remove_button.setEnabled(self.registered_list.currentItem() is not None)

    def register_new_window(self) -> None:
        dialog = SessionManagerDialog(self._registry, self)

        def registered() -> None:
            self._refresh_registered(dialog.session_ref_output.text() or None)
            self.sessions_changed.emit()

        dialog.sessions_changed.connect(registered)
        dialog.exec()
        self._refresh_registered(dialog.session_ref_output.text() or None)
        dialog.deleteLater()


class SessionManagerDialog(_ReadyDialog):
    sessions_changed = Signal()

    def __init__(self, registry: SessionRegistry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._registry = registry
        self._registered_selection: tuple[str, str, str] | None = None
        self.setWindowTitle("注册新窗口")
        self.setMinimumWidth(590)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        title = QLabel("注册已手动登录的比特浏览器 Profile")
        title.setObjectName("dialogTitle")
        copy = QLabel(
            "PostDrop 会生成 session_ref。抖音、小红书和 X 下载时仅在内存中使用对应平台登录态；"
            "Telegram 直接复用已登录的 Web 页面。媒体流量使用该窗口配置的代理（noproxy 时直连）。"
            "不会读取密码，也不会修改代理或指纹。"
        )
        copy.setObjectName("dialogCopy")
        copy.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(copy)

        layout.addWidget(_option_label("社媒平台"))
        self.platform_combo = QComboBox()
        self.platform_combo.setObjectName("optionControl")
        for platform, label in SESSION_PLATFORM_LABELS.items():
            self.platform_combo.addItem(label, platform)
        layout.addWidget(self.platform_combo)

        layout.addWidget(_option_label("比特浏览器本地 API 地址"))
        api_row = QHBoxLayout()
        self.api_url_input = QLineEdit()
        self.api_url_input.setObjectName("sessionInput")
        self.api_url_input.setPlaceholderText("从比特浏览器「系统设置 → 本地 API」复制，例如 http://127.0.0.1:端口")
        previous = self._registry.list()
        self.api_url_input.setText(
            os.getenv("BITBROWSER_API_URL", previous[-1].api_url if previous else "")
        )
        self.load_profiles_button = QPushButton("读取 Profile")
        self.load_profiles_button.setObjectName("secondaryButton")
        self.load_profiles_button.clicked.connect(self.load_profiles)
        api_row.addWidget(self.api_url_input, 1)
        api_row.addWidget(self.load_profiles_button)
        layout.addLayout(api_row)

        layout.addWidget(_option_label("已在所选平台手动登录的 Profile"))
        profile_row = QHBoxLayout()
        self.profile_combo = QComboBox()
        self.profile_combo.setObjectName("optionControl")
        self.profile_combo.addItem("请先读取 Profile", None)
        self.register_button = QPushButton("生成 session_ref")
        self.register_button.setObjectName("secondaryButton")
        self.register_button.clicked.connect(self.register_profile)
        profile_row.addWidget(self.profile_combo, 1)
        profile_row.addWidget(self.register_button)
        layout.addLayout(profile_row)

        layout.addWidget(_option_label("刚生成的 session_ref"))
        ref_row = QHBoxLayout()
        self.session_ref_output = QLineEdit()
        self.session_ref_output.setObjectName("sessionInput")
        self.session_ref_output.setReadOnly(True)
        self.session_ref_output.setPlaceholderText("注册成功后显示，可复制给 Agent 调用")
        copy_button = QPushButton("复制")
        copy_button.setObjectName("secondaryButton")
        copy_button.clicked.connect(self.copy_session_ref)
        ref_row.addWidget(self.session_ref_output, 1)
        ref_row.addWidget(copy_button)
        layout.addLayout(ref_row)

        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("secondaryButton")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        self.finish_button = QPushButton("注册并完成")
        self.finish_button.setObjectName("primaryButton")
        self.finish_button.setDefault(True)
        self.finish_button.clicked.connect(self.register_and_finish)
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_row.addWidget(self.cancel_button)
        close_row.addWidget(self.finish_button)
        layout.addLayout(close_row)
        self.platform_combo.currentIndexChanged.connect(self._invalidate_registration)
        self.profile_combo.currentIndexChanged.connect(self._invalidate_registration)
        self.api_url_input.textChanged.connect(self._invalidate_registration)

    def load_profiles(self) -> None:
        try:
            client = BitBrowserClient(self.api_url_input.text())
            client.health()
            profiles = client.list_profiles()
        except CrawlerError as exc:
            record_exception("social-content", "desktop.handled", exc)
            QMessageBox.warning(self, "无法读取 Profile", str(exc))
            return
        self.profile_combo.clear()
        if not profiles:
            self.profile_combo.addItem("没有可用 Profile", None)
            return
        for profile in profiles:
            self.profile_combo.addItem(f"{profile.name}  ·  {profile.profile_id}", profile.profile_id)

    def register_profile(self) -> bool:
        profile_id = self.profile_combo.currentData()
        platform = str(self.platform_combo.currentData())
        platform_label = SESSION_PLATFORM_LABELS[platform]
        if not profile_id:
            QMessageBox.warning(
                self,
                "尚未选择 Profile",
                f"请先读取并选择一个已登录 {platform_label} 的 Profile。",
            )
            return False
        selection = (platform, self.api_url_input.text().strip(), str(profile_id))
        try:
            # An optional earlier "generate" click already registered this exact
            # selection. Finishing must not rotate the reference a second time.
            if self._registered_selection == selection and any(
                item.session_ref == self.session_ref_output.text()
                for item in self._registry.list()
            ):
                return True
            record = self._registry.register_bitbrowser(
                platform,
                selection[1],
                str(profile_id),
            )
        except (CrawlerError, OSError) as exc:
            record_exception("social-content", "desktop.handled", exc)
            QMessageBox.warning(self, "注册没有完成", str(exc))
            return False
        self.session_ref_output.setText(record.session_ref)
        self.session_ref_output.selectAll()
        self._registered_selection = selection
        self.finish_button.setText("完成")
        self.sessions_changed.emit()
        return True

    def register_and_finish(self) -> None:
        if self.register_profile():
            self.accept()

    def _invalidate_registration(self) -> None:
        self._registered_selection = None
        self.session_ref_output.clear()
        self.finish_button.setText("注册并完成")

    def copy_session_ref(self) -> None:
        if self.session_ref_output.text():
            QApplication.clipboard().setText(self.session_ref_output.text())

class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        output_root: Path | None = None,
        allowed_domains: frozenset[str] | None = None,
        session_registry_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._output_root = (output_root or default_output_root()).resolve()
        self._allowed_domains = allowed_domains or configured_domains()
        self._session_registry_path = (
            session_registry_path or default_session_registry_path()
        ).expanduser().resolve()
        self._session_registry = SessionRegistry(self._session_registry_path)
        self._worker: DownloadWorker | None = None
        self._last_output: DownloadOutput | None = None
        self.setWindowTitle("PostDrop · 社媒帖子下载器")
        self.resize(960, 760)
        self.setMinimumSize(720, 620)
        self.setAcceptDrops(True)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root_layout.addWidget(scroll)

        content = QWidget()
        content.setObjectName("content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(52, 32, 52, 42)
        content_layout.setSpacing(22)
        scroll.setWidget(content)

        header = QHBoxLayout()
        brand_mark = QLabel("↓")
        brand_mark.setObjectName("brandMark")
        brand = QLabel("PostDrop")
        brand.setObjectName("brand")
        local = QLabel("●  本地运行")
        local.setObjectName("localBadge")
        header.addWidget(brand_mark)
        header.addWidget(brand)
        header.addStretch()
        header.addWidget(local)
        content_layout.addLayout(header)

        hero = QLabel("<span style='color:#f5f6f8'>把喜欢的帖子，</span><br><span style='color:#d8ff52'>稳稳保存下来。</span>")
        hero.setObjectName("hero")
        hero.setTextFormat(Qt.TextFormat.RichText)
        hero.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addSpacing(26)
        content_layout.addWidget(hero)

        subtitle = QLabel("粘贴公开帖子，或选择你已登录且有权访问的平台会话，下载视频、音频、封面和字幕。")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setWordWrap(True)
        content_layout.addWidget(subtitle)

        platforms = QLabel(supported_platform_label())
        platforms.setObjectName("platforms")
        platforms.setAlignment(Qt.AlignmentFlag.AlignCenter)
        platforms.setWordWrap(True)
        content_layout.addWidget(platforms)
        content_layout.addSpacing(10)

        self.form_card = QFrame()
        self.form_card.setObjectName("card")
        card_layout = QVBoxLayout(self.form_card)
        card_layout.setContentsMargins(28, 26, 28, 28)
        card_layout.setSpacing(15)

        card_header = QHBoxLayout()
        card_title_box = QVBoxLayout()
        step = QLabel("01 / 输入地址")
        step.setObjectName("step")
        title = QLabel("下载公开帖子")
        title.setObjectName("cardTitle")
        card_title_box.addWidget(step)
        card_title_box.addWidget(title)
        card_header.addLayout(card_title_box)
        card_header.addStretch()
        safe_note = QLabel("本地保存 · 可选登录会话")
        safe_note.setObjectName("safeNote")
        card_header.addWidget(safe_note)
        card_layout.addLayout(card_header)

        input_label = QLabel("社媒帖子或 Telegram 频道地址")
        input_label.setObjectName("fieldLabel")
        card_layout.addWidget(input_label)

        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        self.url_input = QLineEdit()
        self.url_input.setObjectName("urlInput")
        self.url_input.setPlaceholderText("https://x.com/... 或 https://youtube.com/...")
        self.url_input.setClearButtonEnabled(True)
        self.url_input.returnPressed.connect(self.start_download)
        self.download_button = QPushButton("开始下载  →")
        self.download_button.setObjectName("primaryButton")
        self.download_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_button.clicked.connect(self.start_download)
        input_row.addWidget(self.url_input, 1)
        input_row.addWidget(self.download_button)
        card_layout.addLayout(input_row)

        help_text = QLabel("支持公开 HTTPS 帖子；粘贴 Telegram 频道地址时会通过已登录会话遍历并断点保存。也可以把链接拖进窗口")
        help_text.setObjectName("helpText")
        card_layout.addWidget(help_text)

        options = QGridLayout()
        options.setHorizontalSpacing(12)
        options.setVerticalSpacing(10)
        self.format_combo = QComboBox()
        self.format_combo.addItem("音视频", MediaFormat.BEST)
        self.format_combo.addItem("仅视频", MediaFormat.VIDEO)
        self.format_combo.addItem("仅音频", MediaFormat.AUDIO)
        self.format_combo.setObjectName("optionControl")
        self.size_combo = QComboBox()
        self.size_combo.addItem("200 MB", 200)
        self.size_combo.addItem("500 MB", 500)
        self.size_combo.addItem("1 GB", 1000)
        self.size_combo.setCurrentIndex(1)
        self.size_combo.setObjectName("optionControl")
        self.thumbnail_check = QCheckBox("同时保存封面")
        self.subtitle_check = QCheckBox("同时保存字幕")
        self.browser_session_check = QCheckBox("抖音首次或缓存失效时允许读取浏览器会话")
        self.browser_session_check.setChecked(True)
        self.browser_session_check.setToolTip("成功后仅缓存抖音的匿名站点 Cookie；账号登录 Cookie 和其他网站 Cookie 不会保存")
        self.thumbnail_check.setObjectName("optionCheck")
        self.subtitle_check.setObjectName("optionCheck")
        self.browser_session_check.setObjectName("optionCheck")
        options.addWidget(_option_label("媒体格式"), 0, 0)
        options.addWidget(_option_label("单文件上限"), 0, 1)
        options.addWidget(self.format_combo, 1, 0)
        options.addWidget(self.size_combo, 1, 1)
        options.addWidget(self.thumbnail_check, 2, 0)
        options.addWidget(self.subtitle_check, 2, 1)
        options.addWidget(self.browser_session_check, 3, 0, 1, 2)
        session_label_row = QHBoxLayout()
        session_label_row.addWidget(_option_label("平台登录会话（可选）"))
        session_label_row.addStretch()
        self.manage_sessions_button = QPushButton("管理浏览器窗口")
        self.manage_sessions_button.setObjectName("inlineButton")
        self.manage_sessions_button.clicked.connect(self.manage_sessions)
        session_label_row.addWidget(self.manage_sessions_button)
        session_control = QWidget()
        session_control.setLayout(session_label_row)
        self.session_combo = QComboBox()
        self.session_combo.setObjectName("optionControl")
        options.addWidget(session_control, 4, 0, 1, 2)
        options.addWidget(self.session_combo, 5, 0, 1, 2)
        self._refresh_sessions()
        card_layout.addLayout(options)

        self.status_frame = QFrame()
        self.status_frame.setObjectName("statusFrame")
        self.status_frame.hide()
        status_layout = QVBoxLayout(self.status_frame)
        status_layout.setContentsMargins(16, 14, 16, 14)
        status_top = QHBoxLayout()
        self.status_label = QLabel("正在读取帖子…")
        self.status_label.setObjectName("statusLabel")
        self.status_percent = QLabel("0%")
        self.status_percent.setObjectName("statusPercent")
        status_top.addWidget(self.status_label)
        status_top.addStretch()
        status_top.addWidget(self.status_percent)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setValue(0)
        status_layout.addLayout(status_top)
        status_layout.addWidget(self.progress_bar)
        card_layout.addWidget(self.status_frame)
        content_layout.addWidget(self.form_card)

        self.result_card = QFrame()
        self.result_card.setObjectName("card")
        self.result_card.hide()
        result_layout = QVBoxLayout(self.result_card)
        result_layout.setContentsMargins(28, 26, 28, 28)
        result_layout.setSpacing(13)
        result_header = QHBoxLayout()
        result_title_box = QVBoxLayout()
        result_step = QLabel("02 / 下载结果")
        result_step.setObjectName("step")
        result_title = QLabel("帖子内容已保存")
        result_title.setObjectName("cardTitle")
        result_title_box.addWidget(result_step)
        result_title_box.addWidget(result_title)
        result_header.addLayout(result_title_box)
        result_header.addStretch()
        self.open_folder_button = QPushButton("打开下载目录")
        self.open_folder_button.setObjectName("secondaryButton")
        self.open_folder_button.clicked.connect(self.open_output_folder)
        result_header.addWidget(self.open_folder_button)
        result_layout.addLayout(result_header)

        self.extractor_label = QLabel()
        self.extractor_label.setObjectName("extractor")
        self.post_title = QLabel()
        self.post_title.setObjectName("postTitle")
        self.post_title.setWordWrap(True)
        self.post_meta = QLabel()
        self.post_meta.setObjectName("postMeta")
        self.post_description = QLabel()
        self.post_description.setObjectName("postDescription")
        self.post_description.setWordWrap(True)
        result_layout.addWidget(self.extractor_label)
        result_layout.addWidget(self.post_title)
        result_layout.addWidget(self.post_meta)
        result_layout.addWidget(self.post_description)

        separator = QFrame()
        separator.setObjectName("separator")
        separator.setFrameShape(QFrame.Shape.HLine)
        result_layout.addWidget(separator)
        files_title = QLabel("已下载文件")
        files_title.setObjectName("filesTitle")
        result_layout.addWidget(files_title)
        self.files_layout = QVBoxLayout()
        self.files_layout.setSpacing(8)
        result_layout.addLayout(self.files_layout)

        result_actions = QHBoxLayout()
        result_actions.addStretch()
        new_button = QPushButton("下载另一个帖子")
        new_button.setObjectName("secondaryButton")
        new_button.clicked.connect(self.reset_form)
        result_actions.addWidget(new_button)
        result_layout.addLayout(result_actions)
        content_layout.addWidget(self.result_card)

        guide = QFrame()
        guide.setObjectName("guide")
        guide_layout = QHBoxLayout(guide)
        guide_layout.setContentsMargins(20, 16, 20, 16)
        guide_layout.setSpacing(16)
        for number, heading, copy in (
            ("1", "粘贴地址", "公开社媒帖子链接"),
            ("2", "解析内容", "识别媒体和帖子信息"),
            ("3", "本地保存", "下载到 PostDrop 目录"),
        ):
            guide_layout.addWidget(_guide_item(number, heading, copy), 1)
        content_layout.addWidget(guide)

        footer = QLabel("只下载你有权保存的公开内容，并遵守目标平台规则。")
        footer.setObjectName("footer")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(footer)

    def start_download(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        raw_url = extract_post_url(self.url_input.text())
        self.url_input.setText(raw_url)
        parsed = QUrl(raw_url)
        if not raw_url or parsed.scheme().lower() != "https" or not parsed.host():
            self._show_error("请输入完整的公开 HTTPS 帖子地址。")
            self.url_input.setFocus()
            return
        try:
            request = DownloadInput(
                urls=[raw_url],
                media_format=self.format_combo.currentData(),
                include_playlists=False,
                max_items=1,
                max_file_size_mb=int(self.size_combo.currentData()),
                max_total_size_mb=min(int(self.size_combo.currentData()) * 2, 5_000),
                write_thumbnail=self.thumbnail_check.isChecked(),
                write_subtitles=self.subtitle_check.isChecked(),
                browser_cookie_source=(
                    BrowserCookieSource.AUTO
                    if self.browser_session_check.isChecked()
                    else BrowserCookieSource.NONE
                ),
                session_ref=self.session_combo.currentData(),
                telegram_scope=(
                    "channel" if is_telegram_channel_url(raw_url) else "messages"
                ),
                telegram_max_messages=2_000,
            )
        except ValidationError as exc:
            record_exception("social-content", "desktop.handled", exc)
            self._show_error(str(exc.errors()[0].get("msg", "帖子地址不正确")))
            return

        self.result_card.hide()
        self.status_frame.show()
        self.progress_bar.setValue(2)
        self.status_percent.setText("2%")
        self.status_label.setText("正在读取帖子并查找可下载媒体…")
        self.download_button.setEnabled(False)
        self.download_button.setText("正在处理…")
        self.url_input.setEnabled(False)
        self._worker = DownloadWorker(
            request,
            self._output_root,
            self._allowed_domains,
            self._session_registry_path,
            self,
        )
        self._worker.progress_changed.connect(self._update_progress)
        self._worker.succeeded.connect(self._download_succeeded)
        self._worker.failed.connect(self._download_failed)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def _update_progress(self, value: int, message: str) -> None:
        self.progress_bar.setValue(value)
        self.status_percent.setText(f"{value}%")
        self.status_label.setText(message)

    def _download_succeeded(self, output: DownloadOutput) -> None:
        self._last_output = output
        self._update_progress(100, "下载完成，文件已保存到本机")
        self._render_result(output)

    def _download_failed(self, code: str, message: str) -> None:
        self.status_frame.hide()
        self._show_error(f"{message}\n\n错误代码：{code}")

    def _worker_finished(self) -> None:
        self.download_button.setEnabled(True)
        self.download_button.setText("开始下载  →")
        self.url_input.setEnabled(True)

    def _render_result(self, output: DownloadOutput) -> None:
        item = output.items[0] if output.items else None
        self.extractor_label.setText((item.extractor if item else "SOCIAL MEDIA").upper())
        self.post_title.setText(item.title if item and item.title else "帖子内容")
        meta = []
        if item and item.uploader:
            meta.append(f"作者 · {item.uploader}")
        if item and item.upload_date:
            meta.append(f"发布 · {item.upload_date.isoformat()}")
        if item and item.duration_seconds:
            meta.append(f"时长 · {format_duration(item.duration_seconds)}")
        self.post_meta.setText("     ".join(meta))
        self.post_description.setText(
            item.description[:700]
            if item and item.description
            else "已成功提取公开帖子中的媒体内容。"
        )
        _clear_layout(self.files_layout)
        if not output.artifacts:
            empty = QLabel("已读取帖子信息，但没有产生可下载文件。")
            empty.setObjectName("emptyFiles")
            self.files_layout.addWidget(empty)
        for artifact in output.artifacts:
            self.files_layout.addWidget(_file_row(Path(artifact.path), artifact.size_bytes))
        self.result_card.show()

    def open_output_folder(self) -> None:
        if self._last_output and self._last_output.output_directory:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_output.output_directory))

    def reset_form(self) -> None:
        self.result_card.hide()
        self.status_frame.hide()
        self.url_input.clear()
        self.url_input.setFocus()
        self._last_output = None

    def manage_sessions(self) -> None:
        dialog = RegisteredSessionsDialog(self._session_registry, self)
        dialog.sessions_changed.connect(self._refresh_sessions)
        dialog.exec()
        self._refresh_sessions()

    def _refresh_sessions(self) -> None:
        selected = self.session_combo.currentData() if hasattr(self, "session_combo") else None
        self.session_combo.clear()
        self.session_combo.addItem("不使用登录会话（公开帖子）", None)
        for record in self._session_registry.list():
            platform_label = SESSION_PLATFORM_LABELS.get(record.platform, record.platform)
            self.session_combo.addItem(
                f"{platform_label} · {record.profile_name} · {record.session_ref}",
                record.session_ref,
            )
            if record.session_ref == selected:
                self.session_combo.setCurrentIndex(self.session_combo.count() - 1)

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self, "下载没有完成", message)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        if urls:
            self.url_input.setText(extract_post_url(urls[0].toString()))
            event.acceptProposedAction()

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            answer = QMessageBox.question(
                self,
                "下载仍在进行",
                "关闭窗口会等待当前下载安全结束。是否继续关闭？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._worker.wait()
        event.accept()


def _option_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("optionLabel")
    return label


def _guide_item(number: str, heading: str, copy: str) -> QFrame:
    frame = QFrame()
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(7, 4, 7, 4)
    number_label = QLabel(number)
    number_label.setObjectName("guideNumber")
    text_box = QVBoxLayout()
    title = QLabel(heading)
    title.setObjectName("guideTitle")
    detail = QLabel(copy)
    detail.setObjectName("guideCopy")
    text_box.addWidget(title)
    text_box.addWidget(detail)
    layout.addWidget(number_label)
    layout.addLayout(text_box)
    return frame


def _file_row(path: Path, size_bytes: int) -> QFrame:
    frame = QFrame()
    frame.setObjectName("fileRow")
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 10)
    extension = QLabel((path.suffix.lstrip(".")[:4] or "FILE").upper())
    extension.setObjectName("fileType")
    name_box = QVBoxLayout()
    name = QLabel(path.name)
    name.setObjectName("fileName")
    name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    size = QLabel(format_bytes(size_bytes))
    size.setObjectName("fileSize")
    name_box.addWidget(name)
    name_box.addWidget(size)
    open_button = QPushButton("打开文件")
    open_button.setObjectName("fileButton")
    open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
    layout.addWidget(extension)
    layout.addLayout(name_box, 1)
    layout.addWidget(open_button)
    return frame


def _clear_layout(layout: QVBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget:
            widget.deleteLater()


def default_output_root() -> Path:
    configured = os.getenv("SOCIAL_DOWNLOADER_OUTPUT_ROOT")
    if configured:
        return Path(configured).expanduser()
    downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    return Path(downloads or Path.home() / "Downloads") / APP_NAME


def configured_domains() -> frozenset[str]:
    configured = os.getenv("SOCIAL_DOWNLOADER_ALLOWED_DOMAINS")
    if not configured:
        return DEFAULT_ALLOWED_DOMAINS
    return frozenset(
        value.lower().strip().lstrip(".")
        for value in configured.split(",")
        if value.strip()
    )


def extract_post_url(value: str) -> str:
    """Extract a URL from Chinese share text and upgrade known short links to HTTPS."""

    match = re.search(r"https?://[^\s，。；;！!]+", value.strip(), flags=re.IGNORECASE)
    candidate = (match.group(0) if match else value.strip()).rstrip(")]}>'\"")
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").lower().rstrip(".")
    https_upgrade_domains = {
        "douyin.com",
        "iesdouyin.com",
        "v.douyin.com",
        "xhslink.com",
        "xhslink.cn",
        "xiaohongshu.com",
        "t.me",
        "telegram.me",
        "web.telegram.org",
    }
    if parsed.scheme.lower() == "http" and any(
        host == domain or host.endswith(f".{domain}") for domain in https_upgrade_domains
    ):
        parsed = parsed._replace(scheme="https")
        candidate = urlunsplit(parsed)
    return candidate


def is_telegram_channel_url(value: str) -> bool:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host == "web.telegram.org":
        return bool(parsed.fragment)
    if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        return False
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "c" and parts[1].isdigit() and parts[2].isdigit():
        return False
    return len(parts) == 1 or (len(parts) == 2 and parts[0] == "c" and parts[1].isdigit())


def format_bytes(value: float) -> str:
    size = float(value or 0)
    units = ("B", "KB", "MB", "GB")
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    precision = 0 if index == 0 else 1
    return f"{size:.{precision}f} {units[index]}"


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


STYLESHEET = """
QDialog#browserWindowDialog { background: #15171d; color: #f5f6f8; }
QWidget#root, QWidget#content { background: #0b0c10; color: #f5f6f8; }
QScrollArea { background: #0b0c10; border: none; }
QLabel#brandMark { min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; border-radius: 9px; background: #d8ff52; color: #0b0c10; font-size: 18px; font-weight: 900; qproperty-alignment: AlignCenter; }
QLabel#brand { color: #f5f6f8; font-size: 19px; font-weight: 750; }
QLabel#localBadge { color: #969ba8; font-size: 11px; }
QLabel#hero { font-size: 46px; font-weight: 780; line-height: 0.95; }
QLabel#hero span { color: #d8ff52; }
QLabel#subtitle { color: #aeb2bd; font-size: 14px; }
QLabel#platforms { color: #626773; font-size: 10px; }
QFrame#card { background: #15171d; border: 1px solid #292c34; border-radius: 22px; }
QLabel#step { color: #d8ff52; font-size: 10px; font-weight: 750; letter-spacing: 2px; }
QLabel#cardTitle { color: #f5f6f8; font-size: 23px; font-weight: 750; }
QLabel#safeNote, QLabel#helpText { color: #6e737e; font-size: 10px; }
QLabel#fieldLabel, QLabel#optionLabel { color: #c8cbd2; font-size: 11px; font-weight: 650; }
QLineEdit#urlInput { min-height: 48px; padding: 0 15px; border: 1px solid #373b45; border-radius: 11px; background: #0f1116; color: #f5f6f8; font-size: 13px; selection-background-color: #7d63ff; }
QLineEdit#urlInput:focus { border-color: #d8ff52; }
QPushButton#primaryButton { min-height: 48px; padding: 0 22px; border: none; border-radius: 11px; background: #d8ff52; color: #111307; font-size: 12px; font-weight: 800; }
QPushButton#primaryButton:hover { background: #e5ff7d; }
QPushButton#primaryButton:disabled { background: #788543; color: #272a1d; }
QComboBox#optionControl { min-height: 38px; padding: 0 12px; border: 1px solid #30333c; border-radius: 9px; background: #101217; color: #d7d9de; }
QComboBox#optionControl::drop-down { border: none; width: 25px; }
QComboBox QAbstractItemView { background: #191b22; color: #e7e8eb; selection-background-color: #343844; }
QCheckBox#optionCheck { min-height: 36px; color: #bfc2ca; font-size: 11px; spacing: 9px; }
QCheckBox#optionCheck::indicator { width: 16px; height: 16px; border: 1px solid #474c56; border-radius: 4px; background: #101217; }
QCheckBox#optionCheck::indicator:checked { background: #d8ff52; border-color: #d8ff52; }
QFrame#statusFrame { background: #101217; border: 1px solid #2a2d35; border-radius: 11px; }
QLabel#statusLabel { color: #d7d9df; font-size: 11px; font-weight: 650; }
QLabel#statusPercent { color: #d8ff52; font-size: 10px; }
QProgressBar { min-height: 5px; max-height: 5px; border: none; border-radius: 2px; background: #2a2d34; }
QProgressBar::chunk { border-radius: 2px; background: #d8ff52; }
QPushButton#secondaryButton, QPushButton#fileButton { min-height: 32px; padding: 0 12px; border: 1px solid #343740; border-radius: 8px; background: #1d2027; color: #d6d8de; font-size: 10px; }
QPushButton#secondaryButton:hover, QPushButton#fileButton:hover { background: #292c34; }
QPushButton#inlineButton { min-height: 26px; padding: 0 8px; border: none; background: transparent; color: #d8ff52; font-size: 10px; }
QLineEdit#sessionInput { min-height: 38px; padding: 0 11px; border: 1px solid #30333c; border-radius: 9px; background: #101217; color: #d7d9de; }
QLabel#dialogTitle { color: #f5f6f8; font-size: 18px; font-weight: 750; }
QLabel#dialogCopy { color: #8d929d; font-size: 10px; }
QListWidget#registeredWindows { background: #101217; color: #d7d9de; border: 1px solid #30333c; border-radius: 9px; padding: 6px; font-size: 13px; }
QListWidget#registeredWindows::item { padding: 0 12px; border-radius: 6px; }
QListWidget#registeredWindows::item:selected { background: #30363e; color: #d8ff52; }
QListWidget#registeredWindows::item:hover { background: #292c34; }
QLabel#extractor { color: #d8ff52; font-size: 9px; font-weight: 750; }
QLabel#postTitle { color: #f1f2f4; font-size: 20px; font-weight: 730; }
QLabel#postMeta { color: #727783; font-size: 10px; }
QLabel#postDescription { color: #969ba6; font-size: 11px; }
QFrame#separator { color: #292c34; }
QLabel#filesTitle { color: #dfe1e5; font-size: 12px; font-weight: 700; }
QFrame#fileRow { background: #101217; border: 1px solid #292c34; border-radius: 10px; }
QLabel#fileType { min-width: 36px; max-width: 36px; min-height: 34px; max-height: 34px; border-radius: 8px; background: #d8ff52; color: #171900; font-size: 8px; font-weight: 900; qproperty-alignment: AlignCenter; }
QLabel#fileName { color: #d9dbe0; font-size: 10px; }
QLabel#fileSize, QLabel#emptyFiles { color: #686d78; font-size: 9px; }
QFrame#guide { background: #0e1014; border: 1px solid #24272e; border-radius: 15px; }
QLabel#guideNumber { min-width: 27px; max-width: 27px; min-height: 27px; max-height: 27px; border: 1px solid #353943; border-radius: 13px; color: #d8ff52; font-size: 9px; qproperty-alignment: AlignCenter; }
QLabel#guideTitle { color: #d7d9de; font-size: 10px; font-weight: 700; }
QLabel#guideCopy, QLabel#footer { color: #5f646f; font-size: 8px; }
QToolTip { background: #20232a; color: white; border: 1px solid #373b44; }
QScrollBar:vertical { width: 8px; margin: 0; border: none; background: #0b0c10; }
QScrollBar::handle:vertical { min-height: 36px; border-radius: 4px; background: #343841; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; background: none; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
"""


def main() -> None:
    install_exception_hooks("social-content")
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--output-root")
    parser.add_argument("--manage-sessions-only", action="store_true")
    arguments, qt_arguments = parser.parse_known_args()
    app = QApplication([sys.argv[0], *qt_arguments])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Social Agent")
    app.setStyle("Fusion")
    app.setFont(QFont("Arial", 11))
    app.setStyleSheet(STYLESHEET)
    output_root = Path(arguments.output_root).expanduser() if arguments.output_root else None
    if arguments.manage_sessions_only:
        registry = SessionRegistry(default_session_registry_path())
        dialog = RegisteredSessionsDialog(registry)
        dialog.exec()
        return
    window = MainWindow(output_root=output_root)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
