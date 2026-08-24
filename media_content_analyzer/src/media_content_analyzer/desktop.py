from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import sys
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from PySide6.QtCore import QStandardPaths, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .contracts import (
    AnalyzeContentInput,
    ArtifactRef,
    ContentAnalysisOutput,
    CopyPlatform,
    CopyTone,
    GeneratePostCopyInput,
    GeneratePostCopyOutput,
)
from .errors import AnalyzerError
from .ports import ToolContext
from .runtime import build_local_copy_tool, build_local_tool


APP_NAME = "PostInsight"
SUPPORTED_SUFFIXES = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
        ".mp4",
        ".mov",
        ".mkv",
        ".webm",
        ".avi",
        ".flv",
        ".m4v",
        ".mp3",
        ".m4a",
        ".aac",
        ".wav",
        ".flac",
        ".ogg",
        ".opus",
    }
)


@dataclass(frozen=True)
class AnalysisOptions:
    post_text: str | None
    language_hint: str | None
    generate_summary: bool
    generate_tags: bool
    run_ocr: bool
    transcribe_audio: bool
    run_vision_model: bool
    max_keyframes: int


@dataclass(frozen=True)
class CopyOptions:
    platform: CopyPlatform
    tone: CopyTone
    language: str
    objective: str | None
    extra_instructions: str | None
    variant_count: int
    max_characters: int
    include_hashtags: bool


class AnalysisWorker(QThread):
    progress_changed = Signal(int, str)
    succeeded = Signal(object)
    failed = Signal(str, str)

    def __init__(
        self,
        paths: Sequence[Path],
        options: AnalysisOptions,
        state_root: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._paths = list(paths)
        self._options = options
        self._state_root = state_root

    def run(self) -> None:
        try:
            self.progress_changed.emit(8, "正在校验媒体文件…")
            artifacts = [_artifact_ref(path) for path in self._paths]
            request = AnalyzeContentInput(
                artifacts=artifacts,
                post_text=self._options.post_text,
                language_hint=self._options.language_hint,
                generate_summary=self._options.generate_summary,
                generate_tags=self._options.generate_tags,
                run_ocr=self._options.run_ocr,
                transcribe_audio=self._options.transcribe_audio,
                run_vision_model=self._options.run_vision_model,
                max_keyframes=self._options.max_keyframes,
                force_reanalyze=False,
            )
            self.progress_changed.emit(
                25, "正在提取关键帧、文字和音轨，并调用本地模型…"
            )
            tool = build_local_tool(
                allowed_media_root=_common_parent(self._paths),
                state_root=self._state_root,
                enable_ocr=self._options.run_ocr,
                enable_asr=self._options.transcribe_audio,
                enable_vision=self._options.run_vision_model,
            )
            output = asyncio.run(
                tool.execute(
                    request,
                    ToolContext(
                        tenant_id="local-desktop",
                        trace_id=f"desktop-{uuid.uuid4().hex}",
                        actor_type="user",
                        actor_id=os.getenv("USER")
                        or os.getenv("USERNAME")
                        or "desktop-user",
                    ),
                )
            )
        except AnalyzerError as exc:
            self.failed.emit(str(exc.code), str(exc))
        except Exception as exc:
            self.failed.emit(
                "unexpected_error",
                f"分析没有完成（{type(exc).__name__}）。请检查媒体文件和本地模型。",
            )
        else:
            self.progress_changed.emit(100, "分析完成")
            self.succeeded.emit(output)


class CopyWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str, str)

    def __init__(
        self,
        analysis: ContentAnalysisOutput,
        options: CopyOptions,
        state_root: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._analysis = analysis
        self._options = options
        self._state_root = state_root

    def run(self) -> None:
        try:
            request = GeneratePostCopyInput(
                analysis=self._analysis,
                platform=self._options.platform,
                tone=self._options.tone,
                language=self._options.language,
                objective=self._options.objective,
                extra_instructions=self._options.extra_instructions,
                variant_count=self._options.variant_count,
                max_characters=self._options.max_characters,
                include_hashtags=self._options.include_hashtags,
            )
            tool = build_local_copy_tool(state_root=self._state_root)
            output = asyncio.run(
                tool.execute(
                    request,
                    ToolContext(
                        tenant_id="local-desktop",
                        trace_id=f"desktop-copy-{uuid.uuid4().hex}",
                        actor_type="user",
                        actor_id=os.getenv("USER")
                        or os.getenv("USERNAME")
                        or "desktop-user",
                    ),
                )
            )
        except AnalyzerError as exc:
            self.failed.emit(str(exc.code), str(exc))
        except Exception as exc:
            self.failed.emit(
                "unexpected_error",
                f"文案没有生成（{type(exc).__name__}）。请确认 Ollama 和 qwen3.5:9b 正在运行。",
            )
        else:
            self.succeeded.emit(output)


class MainWindow(QMainWindow):
    def __init__(self, *, state_root: Path | None = None) -> None:
        super().__init__()
        self._state_root = (state_root or default_state_root()).expanduser().resolve()
        self._paths: list[Path] = []
        self._worker: AnalysisWorker | None = None
        self._copy_worker: CopyWorker | None = None
        self._last_output: ContentAnalysisOutput | None = None
        self._last_copy_output: GeneratePostCopyOutput | None = None
        self.setWindowTitle("PostInsight · 社媒内容分析器")
        self.resize(1020, 820)
        self.setMinimumSize(760, 660)
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
        content_layout.setSpacing(20)
        scroll.setWidget(content)

        header = QHBoxLayout()
        brand_mark = QLabel("✦")
        brand_mark.setObjectName("brandMark")
        brand = QLabel("PostInsight")
        brand.setObjectName("brand")
        local = QLabel("●  Qwen3.5-9B · 本地运行")
        local.setObjectName("localBadge")
        header.addWidget(brand_mark)
        header.addWidget(brand)
        header.addStretch()
        header.addWidget(local)
        content_layout.addLayout(header)

        hero = QLabel(
            "<span style='color:#f5f6f8'>看懂帖子，</span><br>"
            "<span style='color:#d8ff52'>提炼真正重要的内容。</span>"
        )
        hero.setObjectName("hero")
        hero.setTextFormat(Qt.TextFormat.RichText)
        hero.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addSpacing(20)
        content_layout.addWidget(hero)

        subtitle = QLabel(
            "选择下载好的图片、视频或音频，本地生成摘要、标签、OCR、字幕和风险判断。"
        )
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setWordWrap(True)
        content_layout.addWidget(subtitle)

        self.form_card = QFrame()
        self.form_card.setObjectName("card")
        form_layout = QVBoxLayout(self.form_card)
        form_layout.setContentsMargins(28, 26, 28, 28)
        form_layout.setSpacing(14)

        card_header = QHBoxLayout()
        title_box = QVBoxLayout()
        step = QLabel("01 / 选择内容")
        step.setObjectName("step")
        title = QLabel("分析本地帖子媒体")
        title.setObjectName("cardTitle")
        title_box.addWidget(step)
        title_box.addWidget(title)
        card_header.addLayout(title_box)
        card_header.addStretch()
        safe_note = QLabel("本地处理 · 不上传云端")
        safe_note.setObjectName("safeNote")
        card_header.addWidget(safe_note)
        form_layout.addLayout(card_header)

        file_label = QLabel("图片 / 视频 / 音频")
        file_label.setObjectName("fieldLabel")
        form_layout.addWidget(file_label)

        file_actions = QHBoxLayout()
        self.choose_button = QPushButton("选择媒体文件")
        self.choose_button.setObjectName("secondaryButton")
        self.choose_button.clicked.connect(self.choose_files)
        self.clear_button = QPushButton("清空")
        self.clear_button.setObjectName("secondaryButton")
        self.clear_button.clicked.connect(self.clear_files)
        file_actions.addWidget(self.choose_button)
        file_actions.addWidget(self.clear_button)
        file_actions.addStretch()
        drag_hint = QLabel("也可以把多个文件拖入窗口")
        drag_hint.setObjectName("helpText")
        file_actions.addWidget(drag_hint)
        form_layout.addLayout(file_actions)

        self.file_list = QListWidget()
        self.file_list.setObjectName("fileList")
        self.file_list.setMinimumHeight(92)
        self.file_list.setMaximumHeight(150)
        self.file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        form_layout.addWidget(self.file_list)

        post_label = QLabel("帖子正文（可选）")
        post_label.setObjectName("fieldLabel")
        form_layout.addWidget(post_label)
        self.post_text = QTextEdit()
        self.post_text.setObjectName("postText")
        self.post_text.setPlaceholderText("粘贴下载器提取到的帖子正文，可提升摘要和标签准确度…")
        self.post_text.setMaximumHeight(100)
        form_layout.addWidget(self.post_text)

        options = QGridLayout()
        options.setHorizontalSpacing(14)
        options.setVerticalSpacing(9)
        self.language_combo = QComboBox()
        self.language_combo.setObjectName("optionControl")
        self.language_combo.addItem("自动识别", None)
        self.language_combo.addItem("中文", "zh")
        self.language_combo.addItem("英文", "en")
        self.keyframes_combo = QComboBox()
        self.keyframes_combo.setObjectName("optionControl")
        self.keyframes_combo.addItem("8 张", 8)
        self.keyframes_combo.addItem("16 张", 16)
        self.keyframes_combo.addItem("24 张", 24)
        self.keyframes_combo.setCurrentIndex(2)
        self.summary_check = _option_check("生成摘要", True)
        self.tags_check = _option_check("生成标签", True)
        self.ocr_check = _option_check("识别画面文字（OCR）", True)
        self.asr_check = _option_check("识别视频/音频语音", True)
        self.vision_check = _option_check("使用 Qwen3.5-9B 分析画面", True)
        options.addWidget(_option_label("输出语言"), 0, 0)
        options.addWidget(_option_label("视频关键帧上限"), 0, 1)
        options.addWidget(self.language_combo, 1, 0)
        options.addWidget(self.keyframes_combo, 1, 1)
        options.addWidget(self.summary_check, 2, 0)
        options.addWidget(self.tags_check, 2, 1)
        options.addWidget(self.ocr_check, 3, 0)
        options.addWidget(self.asr_check, 3, 1)
        options.addWidget(self.vision_check, 4, 0, 1, 2)
        form_layout.addLayout(options)

        self.analyze_button = QPushButton("开始分析  →")
        self.analyze_button.setObjectName("primaryButton")
        self.analyze_button.clicked.connect(self.start_analysis)
        form_layout.addWidget(self.analyze_button)

        self.status_frame = QFrame()
        self.status_frame.setObjectName("statusFrame")
        self.status_frame.hide()
        status_layout = QVBoxLayout(self.status_frame)
        status_layout.setContentsMargins(16, 13, 16, 13)
        status_top = QHBoxLayout()
        self.status_label = QLabel("准备分析…")
        self.status_label.setObjectName("statusLabel")
        self.status_percent = QLabel("0%")
        self.status_percent.setObjectName("statusPercent")
        status_top.addWidget(self.status_label)
        status_top.addStretch()
        status_top.addWidget(self.status_percent)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(False)
        status_layout.addLayout(status_top)
        status_layout.addWidget(self.progress_bar)
        form_layout.addWidget(self.status_frame)
        content_layout.addWidget(self.form_card)

        self.result_card = QFrame()
        self.result_card.setObjectName("card")
        self.result_card.hide()
        result_layout = QVBoxLayout(self.result_card)
        result_layout.setContentsMargins(28, 26, 28, 28)
        result_layout.setSpacing(13)

        result_header = QHBoxLayout()
        result_title_box = QVBoxLayout()
        result_step = QLabel("02 / 分析结果")
        result_step.setObjectName("step")
        result_title = QLabel("帖子内容已经整理完成")
        result_title.setObjectName("cardTitle")
        result_title_box.addWidget(result_step)
        result_title_box.addWidget(result_title)
        result_header.addLayout(result_title_box)
        result_header.addStretch()
        self.save_button = QPushButton("保存 JSON")
        self.save_button.setObjectName("secondaryButton")
        self.save_button.clicked.connect(self.save_result)
        result_header.addWidget(self.save_button)
        result_layout.addLayout(result_header)

        self.result_meta = QLabel()
        self.result_meta.setObjectName("resultMeta")
        self.summary_label = QLabel()
        self.summary_label.setObjectName("summary")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.tags_label = QLabel()
        self.tags_label.setObjectName("tags")
        self.tags_label.setWordWrap(True)
        self.tags_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        result_layout.addWidget(self.result_meta)
        result_layout.addWidget(self.summary_label)
        result_layout.addWidget(self.tags_label)

        tabs = QTabWidget()
        tabs.setObjectName("resultTabs")
        self.details_text = _result_text()
        self.transcript_text = _result_text()
        self.evidence_text = _result_text()
        self.json_text = _result_text()
        tabs.addTab(self.details_text, "实体与主张")
        tabs.addTab(self.transcript_text, "OCR 与字幕")
        tabs.addTab(self.evidence_text, "证据与警告")
        tabs.addTab(self.json_text, "完整 JSON")
        result_layout.addWidget(tabs)

        result_actions = QHBoxLayout()
        self.open_state_button = QPushButton("打开分析数据目录")
        self.open_state_button.setObjectName("secondaryButton")
        self.open_state_button.clicked.connect(self.open_state_directory)
        new_button = QPushButton("分析其他内容")
        new_button.setObjectName("secondaryButton")
        new_button.clicked.connect(self.reset_form)
        result_actions.addWidget(self.open_state_button)
        result_actions.addStretch()
        result_actions.addWidget(new_button)
        result_layout.addLayout(result_actions)
        content_layout.addWidget(self.result_card)

        self.copy_card = QFrame()
        self.copy_card.setObjectName("card")
        self.copy_card.hide()
        copy_layout = QVBoxLayout(self.copy_card)
        copy_layout.setContentsMargins(28, 26, 28, 28)
        copy_layout.setSpacing(13)

        copy_header = QHBoxLayout()
        copy_title_box = QVBoxLayout()
        copy_step = QLabel("03 / 生成文案")
        copy_step.setObjectName("step")
        copy_title = QLabel("把分析结果变成可发布文案")
        copy_title.setObjectName("cardTitle")
        copy_title_box.addWidget(copy_step)
        copy_title_box.addWidget(copy_title)
        copy_header.addLayout(copy_title_box)
        copy_header.addStretch()
        copy_note = QLabel("基于分析结果 · 本地 Qwen 生成")
        copy_note.setObjectName("safeNote")
        copy_header.addWidget(copy_note)
        copy_layout.addLayout(copy_header)

        copy_options = QGridLayout()
        copy_options.setHorizontalSpacing(14)
        copy_options.setVerticalSpacing(9)
        self.copy_platform_combo = QComboBox()
        self.copy_platform_combo.setObjectName("optionControl")
        for label, value in (
            ("通用", CopyPlatform.GENERIC),
            ("抖音", CopyPlatform.DOUYIN),
            ("小红书", CopyPlatform.XIAOHONGSHU),
            ("B站", CopyPlatform.BILIBILI),
            ("微博", CopyPlatform.WEIBO),
            ("Instagram", CopyPlatform.INSTAGRAM),
            ("TikTok", CopyPlatform.TIKTOK),
        ):
            self.copy_platform_combo.addItem(label, value)
        self.copy_tone_combo = QComboBox()
        self.copy_tone_combo.setObjectName("optionControl")
        for label, value in (
            ("自然", CopyTone.NATURAL),
            ("种草推荐", CopyTone.RECOMMENDATION),
            ("专业", CopyTone.PROFESSIONAL),
            ("幽默", CopyTone.HUMOROUS),
            ("情绪共鸣", CopyTone.EMOTIONAL),
            ("暧昧吸睛（非露骨）", CopyTone.SUGGESTIVE),
        ):
            self.copy_tone_combo.addItem(label, value)
        self.copy_count_combo = QComboBox()
        self.copy_count_combo.setObjectName("optionControl")
        for count in (1, 3, 5):
            self.copy_count_combo.addItem(f"{count} 条", count)
        self.copy_count_combo.setCurrentIndex(1)
        self.copy_length_combo = QComboBox()
        self.copy_length_combo.setObjectName("optionControl")
        for label, length in (("短 · 100 字", 100), ("中 · 300 字", 300), ("长 · 600 字", 600)):
            self.copy_length_combo.addItem(label, length)
        self.copy_length_combo.setCurrentIndex(1)
        copy_options.addWidget(_option_label("目标平台"), 0, 0)
        copy_options.addWidget(_option_label("文案语气"), 0, 1)
        copy_options.addWidget(self.copy_platform_combo, 1, 0)
        copy_options.addWidget(self.copy_tone_combo, 1, 1)
        copy_options.addWidget(_option_label("生成数量"), 2, 0)
        copy_options.addWidget(_option_label("单条长度"), 2, 1)
        copy_options.addWidget(self.copy_count_combo, 3, 0)
        copy_options.addWidget(self.copy_length_combo, 3, 1)
        copy_layout.addLayout(copy_options)

        self.copy_hashtags_check = _option_check("自动生成话题标签", True)
        copy_layout.addWidget(self.copy_hashtags_check)
        self.copy_objective = QTextEdit()
        self.copy_objective.setObjectName("postText")
        self.copy_objective.setPlaceholderText("发布目标（可选），例如：提升收藏、引导评论、介绍新品…")
        self.copy_objective.setMaximumHeight(68)
        copy_layout.addWidget(self.copy_objective)
        self.copy_instructions = QTextEdit()
        self.copy_instructions.setObjectName("postText")
        self.copy_instructions.setPlaceholderText("补充要求（可选），例如：避免夸张词、突出三个卖点…")
        self.copy_instructions.setMaximumHeight(82)
        copy_layout.addWidget(self.copy_instructions)

        self.generate_copy_button = QPushButton("生成文案  →")
        self.generate_copy_button.setObjectName("primaryButton")
        self.generate_copy_button.clicked.connect(self.start_copy_generation)
        copy_layout.addWidget(self.generate_copy_button)
        self.copy_status = QLabel()
        self.copy_status.setObjectName("statusLabel")
        self.copy_status.hide()
        copy_layout.addWidget(self.copy_status)
        self.copy_result_text = _result_text()
        self.copy_result_text.setMinimumHeight(240)
        self.copy_result_text.hide()
        copy_layout.addWidget(self.copy_result_text)
        copy_actions = QHBoxLayout()
        self.copy_clipboard_button = QPushButton("复制全部文案")
        self.copy_clipboard_button.setObjectName("secondaryButton")
        self.copy_clipboard_button.clicked.connect(self.copy_generated_text)
        self.copy_clipboard_button.hide()
        self.save_copy_button = QPushButton("保存文案 JSON")
        self.save_copy_button.setObjectName("secondaryButton")
        self.save_copy_button.clicked.connect(self.save_copy_result)
        self.save_copy_button.hide()
        copy_actions.addWidget(self.copy_clipboard_button)
        copy_actions.addWidget(self.save_copy_button)
        copy_actions.addStretch()
        copy_layout.addLayout(copy_actions)
        content_layout.addWidget(self.copy_card)

        footer = QLabel(
            "模型结果可能存在误差；低置信度或证据冲突的内容应由人工复核。"
        )
        footer.setObjectName("footer")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(footer)
        self._refresh_file_list()

    def choose_files(self) -> None:
        selected, _ = QFileDialog.getOpenFileNames(
            self,
            "选择要分析的媒体",
            str(Path.home()),
            (
                "媒体文件 (*.jpg *.jpeg *.png *.webp *.gif *.bmp *.tif *.tiff "
                "*.mp4 *.mov *.mkv *.webm *.avi *.flv *.m4v "
                "*.mp3 *.m4a *.aac *.wav *.flac *.ogg *.opus);;所有文件 (*)"
            ),
        )
        self.add_files(Path(value) for value in selected)

    def add_files(self, paths: Iterable[Path]) -> None:
        values = list(paths)
        existing = {path.resolve() for path in self._paths}
        for raw_path in values:
            path = Path(raw_path).expanduser()
            try:
                resolved = path.resolve(strict=True)
            except (FileNotFoundError, OSError):
                continue
            if not resolved.is_file() or not is_supported_media(resolved):
                continue
            if resolved not in existing:
                existing.add(resolved)
                self._paths.append(resolved)
        self._refresh_file_list()

    def clear_files(self) -> None:
        self._paths.clear()
        self._refresh_file_list()

    def _refresh_file_list(self) -> None:
        self.file_list.clear()
        for path in self._paths:
            self.file_list.addItem(f"{path.name}    ·    {format_bytes(path.stat().st_size)}")
        if not self._paths:
            self.file_list.addItem("尚未选择媒体文件")
            self.file_list.item(0).setFlags(Qt.ItemFlag.NoItemFlags)

    def start_analysis(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        if not self._paths:
            self._show_error("请先选择至少一个图片、视频或音频文件。")
            return
        options = AnalysisOptions(
            post_text=self.post_text.toPlainText().strip() or None,
            language_hint=self.language_combo.currentData(),
            generate_summary=self.summary_check.isChecked(),
            generate_tags=self.tags_check.isChecked(),
            run_ocr=self.ocr_check.isChecked(),
            transcribe_audio=self.asr_check.isChecked(),
            run_vision_model=self.vision_check.isChecked(),
            max_keyframes=int(self.keyframes_combo.currentData()),
        )
        self.result_card.hide()
        self.status_frame.show()
        self._update_progress(3, "正在准备分析任务…")
        self.analyze_button.setEnabled(False)
        self.analyze_button.setText("正在分析…")
        self.choose_button.setEnabled(False)
        self._worker = AnalysisWorker(
            self._paths,
            options,
            self._state_root,
            self,
        )
        self._worker.progress_changed.connect(self._update_progress)
        self._worker.succeeded.connect(self._analysis_succeeded)
        self._worker.failed.connect(self._analysis_failed)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def _update_progress(self, value: int, message: str) -> None:
        self.progress_bar.setValue(value)
        self.status_percent.setText(f"{value}%")
        self.status_label.setText(message)

    def _analysis_succeeded(self, output: ContentAnalysisOutput) -> None:
        self._last_output = output
        self._last_copy_output = None
        self._render_result(output)
        self.copy_result_text.hide()
        self.copy_clipboard_button.hide()
        self.save_copy_button.hide()
        self.copy_status.hide()
        self.copy_card.show()

    def _analysis_failed(self, code: str, message: str) -> None:
        self.status_frame.hide()
        self._show_error(f"{message}\n\n错误代码：{code}")

    def _worker_finished(self) -> None:
        self.analyze_button.setEnabled(True)
        self.analyze_button.setText("开始分析  →")
        self.choose_button.setEnabled(True)

    def _render_result(self, output: ContentAnalysisOutput) -> None:
        review = "需要人工复核" if output.needs_human_review else "无需人工复核"
        cache = " · 缓存结果" if output.cache_hit else ""
        self.result_meta.setText(
            f"语言 · {output.language}     情感 · {output.sentiment}     "
            f"置信度 · {output.confidence:.0%}     {review}{cache}"
        )
        self.summary_label.setText(output.summary or "未要求生成摘要。")
        rendered_tags = [f"#{tag.namespace.value}:{tag.label}" for tag in output.tags]
        self.tags_label.setText("   ".join(rendered_tags) or "未生成标签。")

        details = []
        if output.topics:
            details.append("主题\n" + "、".join(output.topics))
        if output.entities:
            details.append("实体\n" + "、".join(output.entities))
        if output.claims:
            details.append("主张\n" + "\n".join(f"• {item}" for item in output.claims))
        if output.commercial_intent:
            details.append(f"商业意图\n{output.commercial_intent}")
        if output.safety_flags:
            details.append("安全标记\n" + "、".join(output.safety_flags))
        self.details_text.setPlainText("\n\n".join(details) or "没有额外实体或主张。")

        media_text = []
        for index, asset in enumerate(output.assets, start=1):
            if asset.ocr_text:
                media_text.append(
                    f"文件 {index} · OCR\n" + "\n".join(asset.ocr_text)
                )
            if asset.transcript:
                lines = [
                    f"[{format_duration(segment.start_seconds)}] {segment.text}"
                    for segment in asset.transcript
                ]
                media_text.append(f"文件 {index} · 字幕\n" + "\n".join(lines))
        self.transcript_text.setPlainText(
            "\n\n".join(media_text) or "没有识别到 OCR 文字或语音字幕。"
        )

        evidence = [
            f"[{item.evidence_id} · {item.kind}] {item.text or ''}".rstrip()
            for item in output.evidence
        ]
        if output.warnings:
            evidence.append("警告\n" + "\n".join(f"• {item}" for item in output.warnings))
        self.evidence_text.setPlainText("\n".join(evidence) or "没有额外证据或警告。")
        self.json_text.setPlainText(output.model_dump_json(indent=2))
        self.result_card.show()

    def save_result(self) -> None:
        if self._last_output is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存分析结果",
            str(Path.home() / "post-analysis.json"),
            "JSON 文件 (*.json)",
        )
        if not path:
            return
        Path(path).expanduser().write_text(
            self._last_output.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )

    def start_copy_generation(self) -> None:
        if self._last_output is None:
            self._show_error("请先完成帖子内容分析。")
            return
        if self._copy_worker and self._copy_worker.isRunning():
            return
        options = CopyOptions(
            platform=self.copy_platform_combo.currentData(),
            tone=self.copy_tone_combo.currentData(),
            language=self.language_combo.currentData() or self._last_output.language or "zh",
            objective=self.copy_objective.toPlainText().strip() or None,
            extra_instructions=self.copy_instructions.toPlainText().strip() or None,
            variant_count=int(self.copy_count_combo.currentData()),
            max_characters=int(self.copy_length_combo.currentData()),
            include_hashtags=self.copy_hashtags_check.isChecked(),
        )
        self.generate_copy_button.setEnabled(False)
        self.generate_copy_button.setText("正在生成…")
        self.copy_status.setText("正在调用本地 qwen3.5:9b 生成文案…")
        self.copy_status.show()
        self.copy_result_text.hide()
        self.copy_clipboard_button.hide()
        self.save_copy_button.hide()
        self._copy_worker = CopyWorker(
            self._last_output, options, self._state_root, self
        )
        self._copy_worker.succeeded.connect(self._copy_succeeded)
        self._copy_worker.failed.connect(self._copy_failed)
        self._copy_worker.finished.connect(self._copy_worker_finished)
        self._copy_worker.start()

    def _copy_succeeded(self, output: GeneratePostCopyOutput) -> None:
        self._last_copy_output = output
        self.copy_result_text.setPlainText(_format_generated_copy(output))
        self.copy_result_text.show()
        self.copy_clipboard_button.show()
        self.save_copy_button.show()
        review = " · 建议人工复核" if output.needs_human_review else ""
        self.copy_status.setText(
            f"已生成 {len(output.variants)} 条文案 · {output.model_version}{review}"
        )

    def _copy_failed(self, code: str, message: str) -> None:
        self.copy_status.setText(f"生成失败 · {code}")
        self._show_error(f"{message}\n\n错误代码：{code}")

    def _copy_worker_finished(self) -> None:
        self.generate_copy_button.setEnabled(True)
        self.generate_copy_button.setText("生成文案  →")

    def copy_generated_text(self) -> None:
        text = self.copy_result_text.toPlainText()
        if text:
            QApplication.clipboard().setText(text)
            self.copy_status.setText("文案已复制到剪贴板")

    def save_copy_result(self) -> None:
        if self._last_copy_output is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存生成文案",
            str(Path.home() / "generated-post-copy.json"),
            "JSON 文件 (*.json)",
        )
        if not path:
            return
        Path(path).expanduser().write_text(
            self._last_copy_output.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )

    def open_state_directory(self) -> None:
        self._state_root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._state_root)))

    def reset_form(self) -> None:
        self.result_card.hide()
        self.copy_card.hide()
        self.status_frame.hide()
        self._last_output = None
        self._last_copy_output = None
        self.clear_files()
        self.post_text.clear()

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self, "分析没有完成", message)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        self.add_files(
            [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        )
        event.acceptProposedAction()

    def closeEvent(self, event) -> None:
        running = (self._worker and self._worker.isRunning()) or (
            self._copy_worker and self._copy_worker.isRunning()
        )
        if running:
            answer = QMessageBox.question(
                self,
                "分析仍在进行",
                "关闭窗口会等待当前分析安全结束。是否继续关闭？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if self._worker and self._worker.isRunning():
                self._worker.wait()
            if self._copy_worker and self._copy_worker.isRunning():
                self._copy_worker.wait()
        event.accept()


def is_supported_media(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_SUFFIXES


def default_state_root() -> Path:
    configured = os.getenv("CONTENT_ANALYZER_STATE_ROOT")
    if configured:
        return Path(configured).expanduser()
    location = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    return Path(location or Path.home() / ".media-content-analyzer")


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
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, value = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{value:02d}"
    return f"{minutes}:{value:02d}"


def _artifact_ref(path: Path) -> ArtifactRef:
    return ArtifactRef(
        path=str(path),
        size_bytes=path.stat().st_size,
        sha256=_sha256(path),
        media_type=mimetypes.guess_type(path.name)[0],
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _common_parent(paths: Sequence[Path]) -> Path:
    return Path(os.path.commonpath([str(path.parent) for path in paths])).resolve()


def _option_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("optionLabel")
    return label


def _option_check(text: str, checked: bool) -> QCheckBox:
    checkbox = QCheckBox(text)
    checkbox.setObjectName("optionCheck")
    checkbox.setChecked(checked)
    return checkbox


def _result_text() -> QPlainTextEdit:
    editor = QPlainTextEdit()
    editor.setReadOnly(True)
    editor.setMinimumHeight(170)
    return editor


def _format_generated_copy(output: GeneratePostCopyOutput) -> str:
    blocks: list[str] = []
    for index, item in enumerate(output.variants, start=1):
        lines = [f"方案 {index}"]
        if item.title:
            lines.append(item.title)
        lines.append(item.body)
        if item.hashtags:
            lines.append(" ".join(f"#{tag.lstrip('#')}" for tag in item.hashtags))
        if item.call_to_action:
            lines.append(item.call_to_action)
        blocks.append("\n".join(lines))
    if output.warnings:
        blocks.append("提示\n" + "\n".join(f"• {item}" for item in output.warnings))
    return "\n\n──────────\n\n".join(blocks)


STYLESHEET = """
QWidget#root, QWidget#content { background: #0b0c10; color: #f5f6f8; }
QScrollArea { background: #0b0c10; border: none; }
QLabel#brandMark { min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; border-radius: 9px; background: #d8ff52; color: #0b0c10; font-size: 17px; font-weight: 900; qproperty-alignment: AlignCenter; }
QLabel#brand { color: #f5f6f8; font-size: 19px; font-weight: 750; }
QLabel#localBadge { color: #969ba8; font-size: 11px; }
QLabel#hero { font-size: 42px; font-weight: 780; }
QLabel#subtitle { color: #aeb2bd; font-size: 14px; }
QFrame#card { background: #15171d; border: 1px solid #292c34; border-radius: 22px; }
QLabel#step { color: #d8ff52; font-size: 10px; font-weight: 750; letter-spacing: 2px; }
QLabel#cardTitle { color: #f5f6f8; font-size: 23px; font-weight: 750; }
QLabel#safeNote, QLabel#helpText { color: #6e737e; font-size: 10px; }
QLabel#fieldLabel, QLabel#optionLabel { color: #c8cbd2; font-size: 11px; font-weight: 650; }
QListWidget#fileList, QTextEdit#postText, QPlainTextEdit { border: 1px solid #343842; border-radius: 10px; background: #0f1116; color: #dfe1e5; padding: 9px; selection-background-color: #596732; }
QListWidget#fileList::item { min-height: 26px; color: #cdd0d7; }
QTextEdit#postText:focus, QListWidget#fileList:focus { border-color: #d8ff52; }
QPushButton#primaryButton { min-height: 48px; padding: 0 22px; border: none; border-radius: 11px; background: #d8ff52; color: #111307; font-size: 12px; font-weight: 800; }
QPushButton#primaryButton:hover { background: #e5ff7d; }
QPushButton#primaryButton:disabled { background: #788543; color: #272a1d; }
QPushButton#secondaryButton { min-height: 32px; padding: 0 13px; border: 1px solid #343740; border-radius: 8px; background: #1d2027; color: #d6d8de; font-size: 10px; }
QPushButton#secondaryButton:hover { background: #292c34; }
QComboBox#optionControl { min-height: 38px; padding: 0 12px; border: 1px solid #30333c; border-radius: 9px; background: #101217; color: #d7d9de; }
QComboBox#optionControl::drop-down { border: none; width: 25px; }
QComboBox QAbstractItemView { background: #191b22; color: #e7e8eb; selection-background-color: #343844; }
QCheckBox#optionCheck { min-height: 34px; color: #bfc2ca; font-size: 11px; spacing: 9px; }
QCheckBox#optionCheck::indicator { width: 16px; height: 16px; border: 1px solid #474c56; border-radius: 4px; background: #101217; }
QCheckBox#optionCheck::indicator:checked { background: #d8ff52; border-color: #d8ff52; }
QFrame#statusFrame { background: #101217; border: 1px solid #2a2d35; border-radius: 11px; }
QLabel#statusLabel { color: #d7d9df; font-size: 11px; font-weight: 650; }
QLabel#statusPercent { color: #d8ff52; font-size: 10px; }
QProgressBar { min-height: 5px; max-height: 5px; border: none; border-radius: 2px; background: #2a2d34; }
QProgressBar::chunk { border-radius: 2px; background: #d8ff52; }
QLabel#resultMeta { color: #8f949f; font-size: 10px; }
QLabel#summary { color: #f0f1f3; font-size: 17px; font-weight: 620; padding: 8px 0; }
QLabel#tags { color: #d8ff52; font-size: 10px; }
QTabWidget#resultTabs::pane { border: 1px solid #30343d; border-radius: 8px; background: #101217; }
QTabBar::tab { background: #171920; color: #8f949e; padding: 9px 13px; border: 1px solid #292d35; }
QTabBar::tab:selected { color: #d8ff52; background: #22252d; }
QLabel#footer { color: #5f646f; font-size: 9px; }
QToolTip { background: #20232a; color: white; border: 1px solid #373b44; }
QScrollBar:vertical { width: 8px; margin: 0; border: none; background: #0b0c10; }
QScrollBar::handle:vertical { min-height: 36px; border-radius: 4px; background: #343841; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; background: none; }
"""


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-root")
    parser.add_argument("--diagnose-media")
    parser.add_argument("--diagnostics-output")
    parser.add_argument("--diagnose-no-ocr", action="store_true")
    parser.add_argument("--diagnose-no-asr", action="store_true")
    parser.add_argument("--diagnose-no-vision", action="store_true")
    parser.add_argument("--diagnose-generate-copy", action="store_true")
    arguments, qt_arguments = parser.parse_known_args()
    if arguments.diagnose_media:
        if not arguments.diagnostics_output:
            raise SystemExit("--diagnostics-output is required with --diagnose-media")
        _run_headless_diagnostic(arguments)
        return
    app = QApplication([sys.argv[0], *qt_arguments])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Social Agent")
    app.setStyle("Fusion")
    app.setFont(QFont("Arial", 11))
    app.setStyleSheet(STYLESHEET)
    state_root = Path(arguments.state_root).expanduser() if arguments.state_root else None
    window = MainWindow(state_root=state_root)
    window.show()
    sys.exit(app.exec())


def _run_headless_diagnostic(arguments: argparse.Namespace) -> None:
    media_path = Path(arguments.diagnose_media).expanduser().resolve(strict=True)
    output_path = Path(arguments.diagnostics_output).expanduser().resolve()
    state_root = (
        Path(arguments.state_root).expanduser().resolve()
        if arguments.state_root
        else output_path.parent / "state"
    )
    payload: dict[str, object]
    try:
        request = AnalyzeContentInput(
            artifacts=[_artifact_ref(media_path)],
            language_hint="zh",
            run_ocr=not arguments.diagnose_no_ocr,
            transcribe_audio=not arguments.diagnose_no_asr,
            run_vision_model=not arguments.diagnose_no_vision,
            max_keyframes=4,
            force_reanalyze=True,
        )
        tool = build_local_tool(
            allowed_media_root=media_path.parent,
            state_root=state_root,
            enable_ocr=not arguments.diagnose_no_ocr,
            enable_asr=not arguments.diagnose_no_asr,
            enable_vision=not arguments.diagnose_no_vision,
        )
        result = asyncio.run(
            tool.execute(
                request,
                ToolContext(
                    tenant_id="packaged-diagnostics",
                    trace_id=f"diagnostics-{uuid.uuid4().hex}",
                    actor_type="system",
                    actor_id="packaged-self-test",
                ),
            )
        )
        payload = {"ok": True, "result": result.model_dump(mode="json")}
        if arguments.diagnose_generate_copy:
            copy_tool = build_local_copy_tool(state_root=state_root)
            copy_result = asyncio.run(
                copy_tool.execute(
                    GeneratePostCopyInput(
                        analysis=result,
                        platform=CopyPlatform.XIAOHONGSHU,
                        tone=CopyTone.RECOMMENDATION,
                        language="zh",
                        variant_count=1,
                        max_characters=160,
                    ),
                    ToolContext(
                        tenant_id="packaged-diagnostics",
                        trace_id=f"diagnostics-copy-{uuid.uuid4().hex}",
                        actor_type="system",
                        actor_id="packaged-self-test",
                    ),
                )
            )
            payload["copy_result"] = copy_result.model_dump(mode="json")
    except Exception as exc:
        payload = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
