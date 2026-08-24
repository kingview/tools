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
from pathlib import Path
from typing import Iterable, Sequence

from PySide6.QtCore import QPoint, QRect, QStandardPaths, Qt, QThread, QUrl, Signal
from PySide6.QtGui import (
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QImage,
    QMouseEvent,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRubberBand,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .contracts import (
    ArtifactRef,
    ProcessWatermarkInput,
    ProcessWatermarkOutput,
    WatermarkArtifactResult,
    WatermarkMode,
    WatermarkRepairQuality,
    WatermarkRegion,
)
from .errors import AnalyzerError
from .ports import ToolContext
from .watermark_runtime import build_local_watermark_tool
from .watermark_processor import _automatic_sample_frame_count


APP_NAME = "WatermarkStudio"
SUPPORTED_VIDEO_SUFFIXES = frozenset(
    {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
)


class RegionSelectionLabel(QLabel):
    region_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(720, 405)
        self._source_image: QImage | None = None
        self._start: QPoint | None = None
        self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self)

    def set_source_image(self, image: QImage) -> None:
        self._source_image = image.copy()
        self._render()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._render()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() is Qt.MouseButton.LeftButton and self._source_image is not None:
            self._start = event.position().toPoint()
            self._rubber_band.setGeometry(QRect(self._start, self._start))
            self._rubber_band.show()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._start is not None:
            self._rubber_band.setGeometry(
                QRect(self._start, event.position().toPoint()).normalized()
            )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._start is None or self._source_image is None:
            return
        selected = QRect(self._start, event.position().toPoint()).normalized()
        self._start = None
        pixmap = self.pixmap()
        if pixmap is None or selected.width() < 5 or selected.height() < 5:
            self._rubber_band.hide()
            return
        offset_x = (self.width() - pixmap.width()) // 2
        offset_y = (self.height() - pixmap.height()) // 2
        display = QRect(offset_x, offset_y, pixmap.width(), pixmap.height())
        selected = selected.intersected(display)
        if selected.width() < 5 or selected.height() < 5:
            self._rubber_band.hide()
            return
        scale_x = self._source_image.width() / pixmap.width()
        scale_y = self._source_image.height() / pixmap.height()
        self.region_selected.emit(
            WatermarkRegion(
                x=max(0, round((selected.x() - offset_x) * scale_x)),
                y=max(0, round((selected.y() - offset_y) * scale_y)),
                width=max(3, round(selected.width() * scale_x)),
                height=max(3, round(selected.height() * scale_y)),
                confidence=1.0,
            )
        )

    def _render(self) -> None:
        if self._source_image is None or self.width() <= 1 or self.height() <= 1:
            return
        self.setPixmap(
            QPixmap.fromImage(self._source_image).scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


class RegionSelectionDialog(QDialog):
    def __init__(self, video_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._video_path = video_path
        self.selected_region: WatermarkRegion | None = None
        self.setWindowTitle("手动框选水印")
        self.resize(840, 570)
        layout = QVBoxLayout(self)
        note = QLabel(
            "在视频首帧上拖动鼠标框住完整水印。固定水印可直接去除；移动水印请同时开启逐帧跟踪。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.preview = RegionSelectionLabel()
        self.preview.setObjectName("preview")
        self.preview.region_selected.connect(self._region_selected)
        layout.addWidget(self.preview, 1)
        self.selection_text = QLabel("尚未框选")
        self.selection_text.setObjectName("resultMeta")
        layout.addWidget(self.selection_text)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._load_first_frame()

    def _load_first_frame(self) -> None:
        import cv2

        capture = cv2.VideoCapture(str(self._video_path))
        try:
            ok, frame = capture.read()
        finally:
            capture.release()
        if not ok or frame is None:
            raise ValueError("无法读取视频首帧")
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width, channels = rgb.shape
        image = QImage(
            rgb.data,
            width,
            height,
            channels * width,
            QImage.Format.Format_RGB888,
        ).copy()
        self.preview.set_source_image(image)

    def _region_selected(self, region: WatermarkRegion) -> None:
        self.selected_region = region
        self.selection_text.setText(
            f"已框选：x={region.x}, y={region.y}, {region.width} × {region.height} px"
        )
        self.ok_button.setEnabled(True)


class WatermarkWorker(QThread):
    progress_changed = Signal(int, str)
    succeeded = Signal(object)
    failed = Signal(str, str)

    def __init__(
        self,
        paths: Sequence[Path],
        *,
        mode: WatermarkMode,
        authorization_confirmed: bool,
        minimum_confidence: float,
        sample_frames: int | None,
        state_root: Path,
        output_root: Path,
        manual_regions: dict[str, list[WatermarkRegion]] | None = None,
        track_manual_regions: bool = False,
        repair_quality: WatermarkRepairQuality = WatermarkRepairQuality.AUTO,
        temporal_consistency: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._paths = list(paths)
        self._mode = mode
        self._authorization_confirmed = authorization_confirmed
        self._minimum_confidence = minimum_confidence
        self._sample_frames = sample_frames
        self._state_root = state_root
        self._output_root = output_root
        self._manual_regions = manual_regions or {}
        self._track_manual_regions = track_manual_regions
        self._repair_quality = repair_quality
        self._temporal_consistency = temporal_consistency

    def run(self) -> None:
        try:
            self.progress_changed.emit(8, "正在校验视频文件和计算哈希…")
            artifacts = [_artifact_ref(path) for path in self._paths]
            request = ProcessWatermarkInput(
                artifacts=artifacts,
                mode=self._mode,
                authorization_confirmed=self._authorization_confirmed,
                minimum_confidence=self._minimum_confidence,
                sample_frames=self._sample_frames,
                manual_regions=self._manual_regions,
                track_manual_regions=self._track_manual_regions,
                repair_quality=self._repair_quality,
                temporal_consistency=self._temporal_consistency,
            )
            self.progress_changed.emit(28, "正在抽帧并检测固定水印区域…")
            tool = build_local_watermark_tool(
                allowed_media_root=_common_parent(self._paths),
                state_root=self._state_root,
                output_root=self._output_root,
                progress_callback=self._repair_progress,
            )
            if self._mode is WatermarkMode.REMOVE_IF_PRESENT:
                self.progress_changed.emit(45, "检测完成后将逐帧修复水印区域…")
            output = asyncio.run(
                tool.execute(
                    request,
                    ToolContext(
                        tenant_id="local-desktop",
                        trace_id=f"watermark-desktop-{uuid.uuid4().hex}",
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
                f"任务没有完成（{type(exc).__name__}）。请检查视频格式和 FFmpeg。",
            )
        else:
            self.progress_changed.emit(100, "处理完成")
            self.succeeded.emit(output)

    def _repair_progress(self, value: int, message: str) -> None:
        mapped = min(94, 45 + round(max(0, min(100, value)) * 0.49))
        self.progress_changed.emit(mapped, message)


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        state_root: Path | None = None,
        output_root: Path | None = None,
    ) -> None:
        super().__init__()
        self._state_root = (state_root or default_state_root()).expanduser().resolve()
        self._output_root = (output_root or default_output_root()).expanduser().resolve()
        self._paths: list[Path] = []
        self._manual_regions: dict[str, list[WatermarkRegion]] = {}
        self._worker: WatermarkWorker | None = None
        self._last_output: ProcessWatermarkOutput | None = None
        self._preview_row = -1
        self._form_locked = False
        self.setWindowTitle("Watermark Studio · 视频水印处理")
        self.resize(1040, 820)
        self.setMinimumSize(780, 650)
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
        brand = QLabel("Watermark Studio")
        brand.setObjectName("brand")
        local = QLabel("●  OpenCV + FFmpeg · 本地运行")
        local.setObjectName("localBadge")
        header.addWidget(brand_mark)
        header.addWidget(brand)
        header.addStretch()
        header.addWidget(local)
        content_layout.addLayout(header)

        hero = QLabel(
            "<span style='color:#f5f6f8'>检测水印，</span>"
            "<span style='color:#d8ff52'>保留干净副本。</span>"
        )
        hero.setObjectName("hero")
        hero.setTextFormat(Qt.TextFormat.RichText)
        hero.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle = QLabel(
            "拖入本地视频，检测固定边缘水印；获得授权后生成新文件，绝不覆盖原视频。"
        )
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addSpacing(14)
        content_layout.addWidget(hero)
        content_layout.addWidget(subtitle)

        form_card = QFrame()
        form_card.setObjectName("card")
        form_layout = QVBoxLayout(form_card)
        form_layout.setContentsMargins(28, 26, 28, 28)
        form_layout.setSpacing(13)

        card_header = QHBoxLayout()
        title_box = QVBoxLayout()
        step = QLabel("01 / 输入视频")
        step.setObjectName("step")
        title = QLabel("检测或去除视频水印")
        title.setObjectName("cardTitle")
        title_box.addWidget(step)
        title_box.addWidget(title)
        card_header.addLayout(title_box)
        card_header.addStretch()
        safe_note = QLabel("本地处理 · 原文件不变")
        safe_note.setObjectName("safeNote")
        card_header.addWidget(safe_note)
        form_layout.addLayout(card_header)

        file_actions = QHBoxLayout()
        self.choose_button = QPushButton("选择视频")
        self.choose_button.setObjectName("secondaryButton")
        self.choose_button.clicked.connect(self.choose_files)
        self.clear_button = QPushButton("清空")
        self.clear_button.setObjectName("secondaryButton")
        self.clear_button.clicked.connect(self.clear_files)
        self.select_region_button = QPushButton("手动框选水印")
        self.select_region_button.setObjectName("secondaryButton")
        self.select_region_button.clicked.connect(self.select_manual_region)
        file_actions.addWidget(self.choose_button)
        file_actions.addWidget(self.select_region_button)
        file_actions.addWidget(self.clear_button)
        file_actions.addStretch()
        form_layout.addLayout(file_actions)

        self.file_list = QListWidget()
        self.file_list.setObjectName("fileList")
        self.file_list.setMinimumHeight(92)
        self.file_list.setMaximumHeight(150)
        form_layout.addWidget(self.file_list)
        help_text = QLabel("支持 MP4、MOV、MKV、WebM、AVI、M4V；也可以直接拖入窗口。")
        help_text.setObjectName("helpText")
        form_layout.addWidget(help_text)

        output_label = QLabel("输出目录")
        output_label.setObjectName("fieldLabel")
        form_layout.addWidget(output_label)
        output_row = QHBoxLayout()
        self.output_path = QLineEdit(str(self._output_root))
        self.output_path.setObjectName("pathControl")
        self.output_path.setReadOnly(True)
        self.output_button = QPushButton("更改目录")
        self.output_button.setObjectName("secondaryButton")
        self.output_button.clicked.connect(self.choose_output_directory)
        output_row.addWidget(self.output_path, 1)
        output_row.addWidget(self.output_button)
        form_layout.addLayout(output_row)

        options = QGridLayout()
        options.setHorizontalSpacing(14)
        options.setVerticalSpacing(9)
        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("optionControl")
        self.mode_combo.addItem("仅检测水印", WatermarkMode.DETECT_ONLY)
        self.mode_combo.addItem("检测并生成无水印副本", WatermarkMode.REMOVE_IF_PRESENT)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.confidence_combo = QComboBox()
        self.confidence_combo.setObjectName("optionControl")
        for label, value in (("标准 · 72%", 0.72), ("严格 · 82%", 0.82), ("宽松 · 62%", 0.62)):
            self.confidence_combo.addItem(label, value)
        self.frames_display = QLabel("自动 · 每秒 2 帧（36–120 帧）")
        self.frames_display.setObjectName("optionDisplay")
        self.repair_quality_combo = QComboBox()
        self.repair_quality_combo.setObjectName("optionControl")
        self.repair_quality_combo.addItem("自动选择（推荐）", WatermarkRepairQuality.AUTO)
        self.repair_quality_combo.addItem("快速修复", WatermarkRepairQuality.FAST)
        self.repair_quality_combo.addItem(
            "本机时序修复", WatermarkRepairQuality.BALANCED
        )
        self.repair_quality_combo.addItem(
            "AI 高质量修复（Apple / NVIDIA）", WatermarkRepairQuality.HIGH
        )
        options.addWidget(_field_label("处理模式"), 0, 0)
        options.addWidget(_field_label("最低置信度"), 0, 1)
        options.addWidget(self.mode_combo, 1, 0)
        options.addWidget(self.confidence_combo, 1, 1)
        options.addWidget(_field_label("检测抽样"), 2, 0)
        options.addWidget(_field_label("修复质量"), 2, 1)
        options.addWidget(self.frames_display, 3, 0)
        options.addWidget(self.repair_quality_combo, 3, 1)
        form_layout.addLayout(options)

        self.temporal_check = QCheckBox("启用时序一致性，减少修复区域闪烁")
        self.temporal_check.setObjectName("optionCheck")
        self.temporal_check.setChecked(True)
        form_layout.addWidget(self.temporal_check)
        quality_help = QLabel(
            "自动模式按水印面积和运动状态选择；AI 高质量模式在 Apple 使用 CoreML、在 NVIDIA 使用 CUDA，缺少 Worker 时自动回退。"
        )
        quality_help.setObjectName("helpText")
        quality_help.setWordWrap(True)
        form_layout.addWidget(quality_help)

        self.track_region_check = QCheckBox("逐帧跟踪手动框选区域（用于移动水印）")
        self.track_region_check.setObjectName("optionCheck")
        form_layout.addWidget(self.track_region_check)

        self.process_button = QPushButton("开始检测  →")
        self.process_button.setObjectName("primaryButton")
        self.process_button.clicked.connect(self.start_processing)
        form_layout.addWidget(self.process_button)

        self.status_frame = QFrame()
        self.status_frame.setObjectName("statusFrame")
        self.status_frame.hide()
        status_layout = QVBoxLayout(self.status_frame)
        status_layout.setContentsMargins(16, 13, 16, 13)
        status_top = QHBoxLayout()
        self.status_label = QLabel("准备处理…")
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
        content_layout.addWidget(form_card)

        self.result_card = QFrame()
        self.result_card.setObjectName("card")
        self.result_card.hide()
        result_layout = QVBoxLayout(self.result_card)
        result_layout.setContentsMargins(28, 26, 28, 28)
        result_layout.setSpacing(13)
        result_header = QHBoxLayout()
        result_title_box = QVBoxLayout()
        result_step = QLabel("02 / 处理结果")
        result_step.setObjectName("step")
        result_title = QLabel("水印检查已完成")
        result_title.setObjectName("cardTitle")
        result_title_box.addWidget(result_step)
        result_title_box.addWidget(result_title)
        result_header.addLayout(result_title_box)
        result_header.addStretch()
        save_button = QPushButton("保存 JSON")
        save_button.setObjectName("secondaryButton")
        save_button.clicked.connect(self.save_result)
        result_header.addWidget(save_button)
        result_layout.addLayout(result_header)

        self.result_meta = QLabel()
        self.result_meta.setObjectName("resultMeta")
        result_layout.addWidget(self.result_meta)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.result_list = QListWidget()
        self.result_list.setObjectName("resultList")
        self.result_list.currentRowChanged.connect(self._result_selection_changed)
        splitter.addWidget(self.result_list)
        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(9)
        self.preview_label = QLabel("选择结果以预览检测区域")
        self.preview_label.setObjectName("preview")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(250)
        self.preview_label.setWordWrap(True)
        self.detail_text = QPlainTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setMinimumHeight(120)
        preview_layout.addWidget(self.preview_label, 1)
        preview_layout.addWidget(self.detail_text)
        splitter.addWidget(preview_panel)
        splitter.setSizes([290, 570])
        result_layout.addWidget(splitter)

        result_actions = QHBoxLayout()
        self.open_output_button = QPushButton("打开输出目录")
        self.open_output_button.setObjectName("secondaryButton")
        self.open_output_button.clicked.connect(self.open_output_directory)
        self.open_file_button = QPushButton("打开选中文件")
        self.open_file_button.setObjectName("secondaryButton")
        self.open_file_button.clicked.connect(self.open_selected_file)
        new_button = QPushButton("处理其他视频")
        new_button.setObjectName("secondaryButton")
        new_button.clicked.connect(self.reset_form)
        result_actions.addWidget(self.open_output_button)
        result_actions.addWidget(self.open_file_button)
        result_actions.addStretch()
        result_actions.addWidget(new_button)
        result_layout.addLayout(result_actions)
        content_layout.addWidget(self.result_card)

        footer = QLabel(
            "自动检测固定及高置信度移动水印；低置信度、间歇出现或复杂形变可手动框选后跟踪。"
        )
        footer.setObjectName("footer")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer.setWordWrap(True)
        content_layout.addWidget(footer)
        self._refresh_file_list()

    def choose_files(self) -> None:
        if self._form_locked:
            return
        selected, _ = QFileDialog.getOpenFileNames(
            self,
            "选择要检测的视频",
            str(Path.home()),
            "视频文件 (*.mp4 *.mov *.mkv *.webm *.avi *.m4v);;所有文件 (*)",
        )
        self.add_files(Path(value) for value in selected)

    def add_files(self, paths: Iterable[Path]) -> None:
        if self._form_locked:
            return
        existing = {path.resolve() for path in self._paths}
        for raw_path in paths:
            try:
                resolved = Path(raw_path).expanduser().resolve(strict=True)
            except (FileNotFoundError, OSError):
                continue
            if not resolved.is_file() or not is_supported_video(resolved):
                continue
            if resolved not in existing and len(self._paths) < 20:
                existing.add(resolved)
                self._paths.append(resolved)
        self._refresh_file_list()

    def clear_files(self) -> None:
        if self._form_locked:
            return
        self._paths.clear()
        self._manual_regions.clear()
        self.track_region_check.setChecked(False)
        self.select_region_button.setText("手动框选水印")
        self._refresh_file_list()

    def select_manual_region(self) -> None:
        if self._form_locked:
            return
        if not self._paths:
            self._show_error("请先选择一个视频文件，再手动框选水印。")
            return
        row = self.file_list.currentRow()
        path = self._paths[row] if 0 <= row < len(self._paths) else self._paths[0]
        try:
            dialog = RegionSelectionDialog(path, self)
        except Exception:
            self._show_error("无法读取该视频的首帧，请检查视频格式。")
            return
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.selected_region is None:
            return
        self._manual_regions[str(path)] = [dialog.selected_region]
        self.track_region_check.setEnabled(True)
        self.select_region_button.setText("已框选 1 个区域 · 重新框选")

    def _refresh_file_list(self) -> None:
        self.file_list.clear()
        for path in self._paths:
            self.file_list.addItem(f"{path.name}    ·    {format_bytes(path.stat().st_size)}")
        if not self._paths:
            self.file_list.addItem("尚未选择视频文件")
            self.file_list.item(0).setFlags(Qt.ItemFlag.NoItemFlags)
        self._refresh_sample_plan()

    def _refresh_sample_plan(self) -> None:
        if not self._paths:
            self.frames_display.setText("自动 · 每秒 2 帧（36–120 帧）")
            return
        if len(self._paths) > 1:
            self.frames_display.setText("自动 · 每个视频按时长独立计算")
            return
        try:
            import cv2

            capture = cv2.VideoCapture(str(self._paths[0]))
            frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            capture.release()
            duration = frame_count / fps if frame_count > 0 and fps > 0 else 0.0
            count = _automatic_sample_frame_count(duration, frame_count)
            if duration > 0:
                self.frames_display.setText(
                    f"自动 · {count} 帧（视频 {duration:.1f} 秒）"
                )
                return
        except Exception:
            pass
        self.frames_display.setText("自动 · 每秒 2 帧（36–120 帧）")

    def choose_output_directory(self) -> None:
        if self._form_locked:
            return
        selected = QFileDialog.getExistingDirectory(
            self, "选择输出目录", str(self._output_root)
        )
        if selected:
            self._output_root = Path(selected).expanduser().resolve()
            self.output_path.setText(str(self._output_root))

    def _mode_changed(self) -> None:
        remove = WatermarkMode(self.mode_combo.currentData()) is WatermarkMode.REMOVE_IF_PRESENT
        self.process_button.setText("检测并去除  →" if remove else "开始检测  →")

    def _set_form_locked(self, locked: bool) -> None:
        self._form_locked = locked
        editable_controls = (
            self.choose_button,
            self.clear_button,
            self.select_region_button,
            self.file_list,
            self.output_path,
            self.output_button,
            self.mode_combo,
            self.confidence_combo,
            self.frames_display,
            self.repair_quality_combo,
            self.temporal_check,
            self.track_region_check,
            self.process_button,
        )
        for control in editable_controls:
            control.setEnabled(not locked)
        if not locked:
            self._mode_changed()

    def start_processing(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        if not self._paths:
            self._show_error("请先选择至少一个视频文件。")
            return
        mode = WatermarkMode(self.mode_combo.currentData())
        authorized = mode is WatermarkMode.REMOVE_IF_PRESENT
        if mode is WatermarkMode.REMOVE_IF_PRESENT:
            repair_quality = WatermarkRepairQuality(
                self.repair_quality_combo.currentData()
            )
            duration_note = (
                "\n\nAI 高质量修复会逐帧运行模型，较长或高帧率视频可能需要数分钟至数十分钟；运行中会显示真实帧进度和预计剩余时间。"
                if repair_quality is WatermarkRepairQuality.HIGH
                else ""
            )
            answer = QMessageBox.question(
                self,
                "确认生成衍生视频",
                f"将处理 {len(self._paths)} 个视频并在输出目录生成新文件，原文件保持不变。"
                f"{duration_note}\n\n继续吗？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._last_output = None
        self.result_card.hide()
        self.status_frame.show()
        self._update_progress(3, "正在准备水印任务…")
        self._set_form_locked(True)
        self._worker = WatermarkWorker(
            self._paths,
            mode=mode,
            authorization_confirmed=authorized,
            minimum_confidence=float(self.confidence_combo.currentData()),
            sample_frames=None,
            state_root=self._state_root,
            output_root=self._output_root,
            manual_regions=dict(self._manual_regions),
            track_manual_regions=self.track_region_check.isChecked(),
            repair_quality=WatermarkRepairQuality(
                self.repair_quality_combo.currentData()
            ),
            temporal_consistency=self.temporal_check.isChecked(),
            parent=self,
        )
        self._worker.progress_changed.connect(self._update_progress)
        self._worker.succeeded.connect(self._processing_succeeded)
        self._worker.failed.connect(self._processing_failed)
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    def _update_progress(self, value: int, message: str) -> None:
        self.progress_bar.setValue(value)
        self.status_percent.setText(f"{value}%")
        self.status_label.setText(message)

    def _processing_succeeded(self, output: ProcessWatermarkOutput) -> None:
        self._last_output = output
        self.result_meta.setText(
            f"检查 · {len(output.items)} 个视频     "
            f"发现水印 · {output.detected_count}     "
            f"生成副本 · {output.processed_count}"
        )
        self.result_list.clear()
        for item in output.items:
            name = Path(item.original.path).name
            if item.processed_artifact:
                status = "✓ 已生成无水印副本"
            elif item.detected:
                status = "! 发现疑似水印"
            else:
                status = "— 未发现可识别水印"
            if item.needs_human_review:
                status += " · 需复核"
            self.result_list.addItem(f"{name}\n{status}")
        self.open_output_button.setEnabled(bool(output.output_directory))
        self.result_card.show()
        if output.items:
            self.result_list.setCurrentRow(0)

    def _processing_failed(self, code: str, message: str) -> None:
        self.status_frame.hide()
        self._show_error(f"{message}\n\n错误代码：{code}")

    def _worker_finished(self) -> None:
        self._set_form_locked(False)

    def _result_selection_changed(self, row: int) -> None:
        self._preview_row = row
        if self._last_output is None or row < 0 or row >= len(self._last_output.items):
            return
        item = self._last_output.items[row]
        lines = [
            f"文件：{Path(item.original.path).name}",
            f"检测：{'发现水印' if item.detected else '未发现可识别水印'}",
            f"置信度：{item.confidence:.0%}",
            f"区域数量：{len(item.regions)}",
            f"人工复核：{'需要' if item.needs_human_review else '不需要'}",
        ]
        if item.processed_artifact:
            lines.append(f"输出：{item.processed_artifact.path}")
            if item.repair_quality_requested is not None:
                lines.append(f"请求质量：{item.repair_quality_requested.value}")
            if item.repair_quality_applied is not None:
                lines.append(f"实际质量：{item.repair_quality_applied.value}")
            if item.repair_method:
                lines.append(f"修复方法：{item.repair_method}")
            if item.quality_score is not None:
                lines.append(f"质量评分：{item.quality_score:.0%}")
        if item.warnings:
            lines.append("警告：\n" + "\n".join(f"• {value}" for value in item.warnings))
        self.detail_text.setPlainText("\n".join(lines))
        self._render_preview(item)

    def _render_preview(self, item: WatermarkArtifactResult) -> None:
        try:
            import cv2

            capture = cv2.VideoCapture(item.original.path)
            try:
                ok, frame = capture.read()
            finally:
                capture.release()
            if not ok or frame is None:
                raise ValueError("cannot read preview frame")
            for index, region in enumerate(item.regions, start=1):
                cv2.rectangle(
                    frame,
                    (region.x, region.y),
                    (region.x + region.width, region.y + region.height),
                    (82, 255, 216),
                    max(2, frame.shape[1] // 500),
                )
                cv2.putText(
                    frame,
                    f"{index}  {region.confidence:.0%}",
                    (region.x, max(20, region.y - 7)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.55,
                    (82, 255, 216),
                    2,
                    cv2.LINE_AA,
                )
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            height, width, channels = rgb.shape
            image = QImage(
                rgb.data,
                width,
                height,
                channels * width,
                QImage.Format.Format_RGB888,
            ).copy()
            pixmap = QPixmap.fromImage(image).scaled(
                max(320, self.preview_label.width() - 12),
                max(220, self.preview_label.height() - 12),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.preview_label.setPixmap(pixmap)
        except Exception:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("无法生成视频预览，但检测结果仍可查看。")

    def save_result(self) -> None:
        if self._last_output is None:
            return
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "保存水印处理结果",
            str(Path.home() / "watermark-result.json"),
            "JSON 文件 (*.json)",
        )
        if selected:
            Path(selected).expanduser().write_text(
                self._last_output.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )

    def open_output_directory(self) -> None:
        if self._last_output and self._last_output.output_directory:
            path = Path(self._last_output.output_directory)
        else:
            path = self._output_root
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_selected_file(self) -> None:
        if self._last_output is None or self._preview_row < 0:
            return
        item = self._last_output.items[self._preview_row]
        path = Path(
            item.processed_artifact.path if item.processed_artifact else item.original.path
        )
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def reset_form(self) -> None:
        self.result_card.hide()
        self.status_frame.hide()
        self._last_output = None
        self._preview_row = -1
        self.preview_label.setPixmap(QPixmap())
        self.preview_label.setText("选择结果以预览检测区域")
        self.detail_text.clear()
        self.clear_files()

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self, "水印任务没有完成", message)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if not self._form_locked and any(
            url.isLocalFile() for url in event.mimeData().urls()
        ):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        if self._form_locked:
            event.ignore()
            return
        self.add_files(
            Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()
        )
        event.acceptProposedAction()

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            answer = QMessageBox.question(
                self,
                "处理仍在进行",
                "关闭窗口会等待当前视频安全处理完成。是否继续关闭？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._worker.wait()
        event.accept()


def is_supported_video(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES


def default_state_root() -> Path:
    configured = os.getenv("WATERMARK_PROCESSOR_STATE_ROOT")
    if configured:
        return Path(configured).expanduser()
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    return Path(location or Path.home() / ".media-watermark-processor")


def default_output_root() -> Path:
    configured = os.getenv("WATERMARK_PROCESSOR_OUTPUT_ROOT")
    if configured:
        return Path(configured).expanduser()
    movies = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
    return Path(movies or Path.home() / "Movies") / "SocialAgent" / "Watermark"


def format_bytes(value: float) -> str:
    size = float(value or 0)
    units = ("B", "KB", "MB", "GB")
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.{0 if index == 0 else 1}f} {units[index]}"


def _artifact_ref(path: Path) -> ArtifactRef:
    return ArtifactRef(
        path=str(path),
        size_bytes=path.stat().st_size,
        sha256=_sha256(path),
        media_type=mimetypes.guess_type(path.name)[0] or "video/mp4",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _common_parent(paths: Sequence[Path]) -> Path:
    return Path(os.path.commonpath([str(path.parent) for path in paths])).resolve()


def _field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("optionLabel")
    return label


STYLESHEET = """
QWidget#root, QWidget#content { background: #0b0c10; color: #f5f6f8; }
QScrollArea { background: #0b0c10; border: none; }
QLabel#brandMark { min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; border-radius: 9px; background: #d8ff52; color: #0b0c10; font-size: 17px; font-weight: 900; qproperty-alignment: AlignCenter; }
QLabel#brand { color: #f5f6f8; font-size: 19px; font-weight: 750; }
QLabel#localBadge { color: #969ba8; font-size: 11px; }
QLabel#hero { font-size: 38px; font-weight: 780; }
QLabel#subtitle { color: #aeb2bd; font-size: 14px; }
QFrame#card { background: #15171d; border: 1px solid #292c34; border-radius: 22px; }
QLabel#step { color: #d8ff52; font-size: 10px; font-weight: 750; letter-spacing: 2px; }
QLabel#cardTitle { color: #f5f6f8; font-size: 23px; font-weight: 750; }
QLabel#safeNote, QLabel#helpText { color: #6e737e; font-size: 10px; }
QLabel#fieldLabel, QLabel#optionLabel { color: #c8cbd2; font-size: 11px; font-weight: 650; }
QListWidget#fileList, QListWidget#resultList, QPlainTextEdit, QLineEdit#pathControl { border: 1px solid #343842; border-radius: 10px; background: #0f1116; color: #dfe1e5; padding: 9px; selection-background-color: #596732; }
QListWidget#fileList::item { min-height: 26px; color: #cdd0d7; }
QListWidget#resultList::item { min-height: 48px; color: #cdd0d7; }
QLabel#preview { border: 1px solid #30343d; border-radius: 10px; background: #0f1116; color: #777c87; }
QPushButton#primaryButton { min-height: 48px; padding: 0 22px; border: none; border-radius: 11px; background: #d8ff52; color: #111307; font-size: 12px; font-weight: 800; }
QPushButton#primaryButton:hover { background: #e5ff7d; }
QPushButton#primaryButton:disabled { background: #788543; color: #272a1d; }
QPushButton#secondaryButton { min-height: 32px; padding: 0 13px; border: 1px solid #343740; border-radius: 8px; background: #1d2027; color: #d6d8de; font-size: 10px; }
QPushButton#secondaryButton:hover { background: #292c34; }
QComboBox#optionControl { min-height: 38px; padding: 0 12px; border: 1px solid #30333c; border-radius: 9px; background: #101217; color: #d7d9de; }
QComboBox#optionControl::drop-down { border: none; width: 25px; }
QComboBox QAbstractItemView { background: #191b22; color: #e7e8eb; selection-background-color: #343844; }
QLabel#optionDisplay { min-height: 38px; padding: 0 12px; border: 1px solid #30333c; border-radius: 9px; background: #101217; color: #d7d9de; }
QCheckBox#optionCheck { min-height: 34px; color: #bfc2ca; font-size: 11px; spacing: 9px; }
QCheckBox#optionCheck::indicator { width: 16px; height: 16px; border: 1px solid #474c56; border-radius: 4px; background: #101217; }
QCheckBox#optionCheck::indicator:checked { background: #d8ff52; border-color: #d8ff52; }
QFrame#statusFrame { background: #101217; border: 1px solid #2a2d35; border-radius: 11px; }
QLabel#statusLabel { color: #d7d9df; font-size: 11px; font-weight: 650; }
QLabel#statusPercent { color: #d8ff52; font-size: 10px; }
QProgressBar { min-height: 5px; max-height: 5px; border: none; border-radius: 2px; background: #2a2d34; }
QProgressBar::chunk { border-radius: 2px; background: #d8ff52; }
QLabel#resultMeta { color: #8f949f; font-size: 10px; }
QLabel#footer { color: #5f646f; font-size: 9px; }
QSplitter::handle { width: 6px; background: #15171d; }
QToolTip { background: #20232a; color: white; border: 1px solid #373b44; }
QScrollBar:vertical { width: 8px; margin: 0; border: none; background: #0b0c10; }
QScrollBar::handle:vertical { min-height: 36px; border-radius: 4px; background: #343841; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; background: none; }
"""


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-root")
    parser.add_argument("--output-root")
    parser.add_argument("--diagnose-media")
    parser.add_argument("--diagnostics-output")
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--authorized", action="store_true")
    parser.add_argument(
        "--repair-quality",
        choices=[item.value for item in WatermarkRepairQuality],
        default=WatermarkRepairQuality.AUTO.value,
    )
    arguments, qt_arguments = parser.parse_known_args(argv)
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
    output_root = Path(arguments.output_root).expanduser() if arguments.output_root else None
    window = MainWindow(state_root=state_root, output_root=output_root)
    window.show()
    raise SystemExit(app.exec())


def _run_headless_diagnostic(arguments: argparse.Namespace) -> None:
    source = Path(arguments.diagnose_media).expanduser().resolve(strict=True)
    result_path = Path(arguments.diagnostics_output).expanduser().resolve()
    state_root = (
        Path(arguments.state_root).expanduser().resolve()
        if arguments.state_root
        else result_path.parent / "state"
    )
    output_root = (
        Path(arguments.output_root).expanduser().resolve()
        if arguments.output_root
        else result_path.parent / "outputs"
    )
    try:
        request = ProcessWatermarkInput(
            artifacts=[_artifact_ref(source)],
            mode=(WatermarkMode.REMOVE_IF_PRESENT if arguments.remove else WatermarkMode.DETECT_ONLY),
            authorization_confirmed=arguments.authorized,
            repair_quality=WatermarkRepairQuality(arguments.repair_quality),
        )
        tool = build_local_watermark_tool(
            allowed_media_root=source.parent,
            state_root=state_root,
            output_root=output_root,
        )
        output = asyncio.run(
            tool.execute(
                request,
                ToolContext(
                    tenant_id="packaged-diagnostics",
                    trace_id=f"watermark-diagnostics-{uuid.uuid4().hex}",
                    actor_type="system",
                    actor_id="packaged-self-test",
                ),
            )
        )
        payload: dict[str, object] = {"ok": True, "result": output.model_dump(mode="json")}
    except Exception as exc:
        payload = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
