from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from media_content_analyzer.desktop import (
    MainWindow,
    format_bytes,
    format_duration,
    is_supported_media,
)


def test_desktop_window_has_analysis_controls(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(state_root=tmp_path / "state")

    assert "社媒内容分析器" in window.windowTitle()
    assert window.analyze_button.text().startswith("开始分析")
    assert window.vision_check.isChecked()
    assert window.ocr_check.isChecked()
    assert window.asr_check.isChecked()
    assert window.keyframes_combo.currentData() == 24
    assert window.result_card.isHidden()
    assert window.copy_card.isHidden()
    assert window.copy_platform_combo.count() == 7
    assert window.copy_tone_combo.count() == 6
    assert window.copy_count_combo.currentData() == 3
    window.close()
    app.processEvents()


def test_desktop_adds_only_supported_media(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    image = tmp_path / "post.png"
    image.write_bytes(b"image")
    text = tmp_path / "notes.txt"
    text.write_text("not media", encoding="utf-8")
    window = MainWindow(state_root=tmp_path / "state")

    window.add_files([image, text])

    assert window._paths == [image.resolve()]
    assert window.file_list.count() == 1
    assert "post.png" in window.file_list.item(0).text()
    window.close()
    app.processEvents()


def test_desktop_formatters_and_media_detection(tmp_path: Path) -> None:
    assert format_bytes(1_048_576) == "1.0 MB"
    assert format_duration(125) == "2:05"
    assert format_duration(3_725) == "1:02:05"
    assert is_supported_media(tmp_path / "post.MP4")
    assert not is_supported_media(tmp_path / "post.pdf")
