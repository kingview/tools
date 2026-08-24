from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from social_content_crawler.desktop import SessionManagerDialog
from social_content_crawler.sessions import SessionRecord, SessionRegistry, default_session_registry_path

from .contracts import AgentPlan, AgentProgress, AgentRunResult
from .planner import ConversationalPlanner, PlanningError, SelectedSession
from .runtime import SocialOperationsAgent


APP_NAME = "Social Agent"
PLATFORM_LABELS = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "x": "X / Twitter",
}


class PlanWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        message: str,
        session: SelectedSession,
        previous_plan: AgentPlan | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._message = message
        self._session = session
        self._previous_plan = previous_plan

    def run(self) -> None:
        try:
            plan = ConversationalPlanner().create_plan(
                self._message,
                self._session,
                self._previous_plan,
            )
        except PlanningError as exc:
            self.failed.emit(str(exc))
        except Exception:
            self.failed.emit("无法生成执行计划，请检查本地模型或换一种更明确的表达。")
        else:
            self.succeeded.emit(plan)


class ExecutionWorker(QThread):
    progress_changed = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        plan: AgentPlan,
        registry_path: Path,
        output_root: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._plan = plan
        self._registry_path = registry_path
        self._output_root = output_root
        self._cancel_requested = False

    def cancel_after_current_batch(self) -> None:
        self._cancel_requested = True

    def run(self) -> None:
        try:
            registry = SessionRegistry(self._registry_path)
            agent = SocialOperationsAgent.local(
                session_registry=registry,
                output_root=self._output_root,
            )
            result = asyncio.run(
                agent.execute_plan(
                    self._plan,
                    progress=self.progress_changed.emit,
                    should_cancel=lambda: self._cancel_requested,
                    authorization_confirmed=True,
                )
            )
        except Exception as exc:
            message = str(exc).strip() or "Agent 执行失败。"
            self.failed.emit(message)
        else:
            self.succeeded.emit(result)


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        registry_path: Path | None = None,
        output_root: Path | None = None,
    ) -> None:
        super().__init__()
        self._registry_path = (registry_path or default_session_registry_path()).expanduser().resolve()
        self._registry = SessionRegistry(self._registry_path)
        self._output_root = (output_root or default_output_root()).expanduser().resolve()
        self._plan_worker: PlanWorker | None = None
        self._execution_worker: ExecutionWorker | None = None
        self._pending_plan: AgentPlan | None = None
        self._last_plan: AgentPlan | None = None
        self._last_result: AgentRunResult | None = None

        self.setWindowTitle("Social Agent · 社媒任务助手")
        self.resize(1_020, 760)
        self.setMinimumSize(780, 620)
        self._build_ui()
        self._refresh_sessions()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(34, 28, 34, 28)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        eyebrow = QLabel("LOCAL TOOL ORCHESTRATOR")
        eyebrow.setObjectName("eyebrow")
        title = QLabel("Social Agent")
        title.setObjectName("title")
        subtitle = QLabel("用一句话编排帖子浏览与下载；计划确认后才执行。")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(eyebrow)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.new_chat_button = QPushButton("新会话")
        self.new_chat_button.setObjectName("secondaryButton")
        self.new_chat_button.clicked.connect(self.new_conversation)
        header.addWidget(self.new_chat_button)
        layout.addLayout(header)

        session_card = QFrame()
        session_card.setObjectName("card")
        session_layout = QHBoxLayout(session_card)
        session_layout.setContentsMargins(16, 12, 16, 12)
        session_label = QLabel("执行会话")
        session_label.setObjectName("fieldLabel")
        self.session_combo = QComboBox()
        self.session_combo.setObjectName("control")
        self.manage_sessions_button = QPushButton("管理比特浏览器会话")
        self.manage_sessions_button.setObjectName("secondaryButton")
        self.manage_sessions_button.clicked.connect(self.manage_sessions)
        session_layout.addWidget(session_label)
        session_layout.addWidget(self.session_combo, 1)
        session_layout.addWidget(self.manage_sessions_button)
        layout.addWidget(session_card)

        self.chat = QTextBrowser()
        self.chat.setObjectName("chat")
        self.chat.setOpenExternalLinks(False)
        self.chat.setPlaceholderText("例如：通过关键词“web3”在抖音上搜索并下载前100个帖子")
        layout.addWidget(self.chat, 1)

        self.progress_frame = QFrame()
        self.progress_frame.setObjectName("progressFrame")
        progress_layout = QVBoxLayout(self.progress_frame)
        progress_layout.setContentsMargins(14, 10, 14, 10)
        progress_top = QHBoxLayout()
        self.progress_label = QLabel("等待执行")
        self.progress_label.setObjectName("progressLabel")
        self.progress_value = QLabel("0%")
        progress_top.addWidget(self.progress_label)
        progress_top.addStretch()
        progress_top.addWidget(self.progress_value)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(False)
        progress_layout.addLayout(progress_top)
        progress_layout.addWidget(self.progress_bar)
        self.progress_frame.hide()
        layout.addWidget(self.progress_frame)

        input_frame = QFrame()
        input_frame.setObjectName("inputFrame")
        input_layout = QVBoxLayout(input_frame)
        input_layout.setContentsMargins(14, 12, 14, 12)
        self.message_input = QPlainTextEdit()
        self.message_input.setObjectName("messageInput")
        self.message_input.setPlaceholderText("描述任务；后续也可以说“改成前50条”继续调整计划…")
        self.message_input.setMaximumHeight(100)
        input_layout.addWidget(self.message_input)
        action_row = QHBoxLayout()
        hint = QLabel("只读浏览与本地下载 · 不自动登录 · 不执行平台写操作")
        hint.setObjectName("hint")
        self.cancel_button = QPushButton("当前批次后停止")
        self.cancel_button.setObjectName("secondaryButton")
        self.cancel_button.clicked.connect(self.cancel_execution)
        self.cancel_button.hide()
        self.execute_button = QPushButton("确认并执行计划")
        self.execute_button.setObjectName("executeButton")
        self.execute_button.setEnabled(False)
        self.execute_button.clicked.connect(self.execute_plan)
        self.plan_button = QPushButton("生成计划  →")
        self.plan_button.setObjectName("primaryButton")
        self.plan_button.clicked.connect(self.request_plan)
        action_row.addWidget(hint)
        action_row.addStretch()
        action_row.addWidget(self.cancel_button)
        action_row.addWidget(self.execute_button)
        action_row.addWidget(self.plan_button)
        input_layout.addLayout(action_row)
        layout.addWidget(input_frame)

        self._append_agent(
            "请选择一个已经在比特浏览器中手动登录的会话，然后告诉我需要搜索、浏览或下载什么。"
        )

    def request_plan(self) -> None:
        record = self._selected_record()
        if record is None:
            QMessageBox.warning(self, "缺少登录会话", "请先选择或注册一个平台登录会话。")
            return
        message = self.message_input.toPlainText().strip()
        if not message:
            return
        self._append_user(message)
        self.message_input.clear()
        self._set_planning(True)
        session = SelectedSession(
            session_ref=record.session_ref,
            platform=record.platform,
            profile_name=record.profile_name,
        )
        worker = PlanWorker(
            message=message,
            session=session,
            previous_plan=self._last_plan,
            parent=self,
        )
        worker.succeeded.connect(self._plan_succeeded)
        worker.failed.connect(self._plan_failed)
        worker.finished.connect(lambda: self._set_planning(False))
        self._plan_worker = worker
        worker.start()

    def _plan_succeeded(self, plan: AgentPlan) -> None:
        self._pending_plan = plan
        self._last_plan = plan
        self.execute_button.setEnabled(True)
        if plan.remove_watermark:
            action = "浏览、下载并在检测到高置信度静态水印时生成去水印副本"
        else:
            action = "浏览并下载" if plan.download else "仅浏览"
        target = plan.query or plan.user_key or str(plan.start_url or "推荐流")
        batches = (
            (plan.limit + plan.download_batch_size - 1) // plan.download_batch_size
            if plan.download
            else 0
        )
        calls = 1 + batches + (batches if plan.remove_watermark else 0)
        self._append_agent(
            f"计划已生成：{PLATFORM_LABELS[plan.platform.value]} · {action} · “{target}” · "
            f"最多 {plan.limit} 条 · 预计最多 {calls} 次 Tool 调用。\n"
            "点击“确认并执行计划”后开始；在此之前不会访问平台或下载文件。"
        )

    def _plan_failed(self, message: str) -> None:
        self._append_agent(f"无法生成计划：{message}", error=True)

    def execute_plan(self) -> None:
        if self._pending_plan is None or self._execution_worker is not None:
            return
        plan = self._pending_plan
        self._pending_plan = None
        self.execute_button.setEnabled(False)
        self.plan_button.setEnabled(False)
        self.session_combo.setEnabled(False)
        self.manage_sessions_button.setEnabled(False)
        self.cancel_button.show()
        self.progress_frame.show()
        self.progress_bar.setValue(0)
        self._append_agent("计划已确认，开始执行。")
        worker = ExecutionWorker(
            plan=plan,
            registry_path=self._registry_path,
            output_root=self._output_root,
            parent=self,
        )
        worker.progress_changed.connect(self._execution_progress)
        worker.succeeded.connect(self._execution_succeeded)
        worker.failed.connect(self._execution_failed)
        worker.finished.connect(self._execution_finished)
        self._execution_worker = worker
        worker.start()

    def cancel_execution(self) -> None:
        if self._execution_worker is not None:
            self._execution_worker.cancel_after_current_batch()
            self.cancel_button.setEnabled(False)
            self.progress_label.setText("已请求停止，将在当前下载批次结束后停止…")

    def _execution_progress(self, event: AgentProgress) -> None:
        percent = int(event.completed / max(event.total, 1) * 100)
        self.progress_bar.setValue(max(0, min(percent, 100)))
        self.progress_value.setText(f"{percent}%")
        self.progress_label.setText(event.message)

    def _execution_succeeded(self, result: AgentRunResult) -> None:
        self._last_result = result
        state = "已停止" if result.cancelled else "执行完成"
        details = (
            f"{state}：发现 {len(result.discovered_urls)} 条，下载 {result.downloaded_items} 条，"
            f"生成 {result.artifact_count} 个文件，使用 {result.tool_calls_used} 次 Tool 调用。"
        )
        if result.plan.remove_watermark:
            details += (
                f"\n检测到水印 {result.watermark_detected_count} 个，"
                f"生成去水印副本 {result.watermark_processed_count} 个。"
            )
        if result.output_directories:
            details += f"\n保存目录：{result.output_directories[0]}"
        if result.watermark_output_directories:
            details += f"\n去水印副本目录：{result.watermark_output_directories[0]}"
        if result.warnings:
            details += "\n提醒：" + "；".join(result.warnings)
        self._append_agent(details)

    def _execution_failed(self, message: str) -> None:
        self._append_agent(f"执行失败：{message}", error=True)

    def _execution_finished(self) -> None:
        self._execution_worker = None
        self.plan_button.setEnabled(True)
        self.session_combo.setEnabled(True)
        self.manage_sessions_button.setEnabled(True)
        self.cancel_button.hide()
        self.cancel_button.setEnabled(True)

    def manage_sessions(self) -> None:
        dialog = SessionManagerDialog(self._registry, self)
        dialog.sessions_changed.connect(self._refresh_sessions)
        dialog.exec()
        self._refresh_sessions()

    def _refresh_sessions(self) -> None:
        selected = self.session_combo.currentData() if self.session_combo.count() else None
        self.session_combo.clear()
        records = self._registry.list()
        if not records:
            self.session_combo.addItem("尚未注册登录会话", None)
            return
        for record in records:
            self.session_combo.addItem(
                f"{PLATFORM_LABELS.get(record.platform, record.platform)} · {record.profile_name}",
                record.session_ref,
            )
            if record.session_ref == selected:
                self.session_combo.setCurrentIndex(self.session_combo.count() - 1)

    def _selected_record(self) -> SessionRecord | None:
        session_ref = self.session_combo.currentData()
        if not session_ref:
            return None
        return self._registry.get(str(session_ref))

    def new_conversation(self) -> None:
        if self._execution_worker is not None:
            QMessageBox.information(self, "任务执行中", "请先等待当前任务结束或请求停止。")
            return
        self.chat.clear()
        self._pending_plan = None
        self._last_plan = None
        self._last_result = None
        self.execute_button.setEnabled(False)
        self.progress_frame.hide()
        self._append_agent("新会话已开始。请选择执行会话并描述任务。")

    def open_last_output(self) -> None:
        if self._last_result and self._last_result.output_directories:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_result.output_directories[0]))

    def _set_planning(self, planning: bool) -> None:
        self.plan_button.setEnabled(not planning)
        self.session_combo.setEnabled(not planning)
        self.plan_button.setText("正在生成计划…" if planning else "生成计划  →")

    def _append_user(self, message: str) -> None:
        self.chat.append(f"<div class='user'><b>你</b><br>{_html(message)}</div>")

    def _append_agent(self, message: str, *, error: bool = False) -> None:
        css_class = "error" if error else "agent"
        self.chat.append(
            f"<div class='{css_class}'><b>Agent</b><br>{_html(message).replace(chr(10), '<br>')}</div>"
        )


def default_output_root() -> Path:
    downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    base = Path(downloads) if downloads else Path.home() / "Downloads"
    return base / "SocialAgent"


def _html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


STYLESHEET = """
QWidget#root { background: #111318; color: #f1f2f4; }
QLabel#eyebrow { color: #d8ff52; font-size: 11px; font-weight: 800; letter-spacing: 2px; }
QLabel#title { font-size: 34px; font-weight: 800; }
QLabel#subtitle, QLabel#hint { color: #9297a3; }
QLabel#fieldLabel, QLabel#progressLabel { color: #cfd2d8; font-weight: 700; }
QFrame#card, QFrame#inputFrame, QFrame#progressFrame { background: #1b1e25; border: 1px solid #30343e; border-radius: 12px; }
QTextBrowser#chat { background: #15171d; border: 1px solid #30343e; border-radius: 14px; padding: 14px; color: #e7e8eb; font-size: 14px; }
QPlainTextEdit#messageInput { background: transparent; border: none; color: #f4f5f6; font-size: 15px; }
QComboBox#control { min-height: 38px; padding: 0 10px; background: #111318; border: 1px solid #383d48; border-radius: 8px; color: #e9eaed; }
QPushButton { min-height: 38px; padding: 0 15px; border-radius: 9px; font-weight: 700; }
QPushButton#primaryButton, QPushButton#executeButton { background: #d8ff52; color: #15170c; border: none; }
QPushButton#executeButton { background: #b7d943; }
QPushButton#secondaryButton { background: #242832; color: #d8dae0; border: 1px solid #3a3f4b; }
QPushButton:disabled { background: #292c33; color: #686d77; }
QProgressBar { min-height: 7px; max-height: 7px; border: none; border-radius: 3px; background: #30343d; }
QProgressBar::chunk { background: #d8ff52; border-radius: 3px; }
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Social Agent desktop client")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(STYLESHEET)
    window = MainWindow(output_root=args.output_root)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
