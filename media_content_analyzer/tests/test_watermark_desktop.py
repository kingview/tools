from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from media_content_analyzer.contracts import WatermarkMode, WatermarkRepairQuality
from media_content_analyzer.watermark_desktop import (
    MainWindow,
    format_bytes,
    is_supported_video,
)


def test_watermark_desktop_has_expected_controls(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(state_root=tmp_path / "state", output_root=tmp_path / "out")

    assert "视频水印处理" in window.windowTitle()
    assert window.mode_combo.count() == 2
    assert window.repair_quality_combo.count() == 4
    assert (
        WatermarkRepairQuality(window.repair_quality_combo.currentData())
        is WatermarkRepairQuality.AUTO
    )
    assert window.temporal_check.isChecked()
    assert WatermarkMode(window.mode_combo.currentData()) is WatermarkMode.DETECT_ONLY
    assert window.process_button.text().startswith("开始检测")
    assert window.track_region_check.isEnabled()
    assert "自动" in window.frames_display.text()
    window.track_region_check.click()
    assert window.track_region_check.isChecked()
    assert not hasattr(window, "authorization_check")
    assert window.select_region_button.text() == "手动框选水印"
    assert window.result_card.isHidden()

    window.mode_combo.setCurrentIndex(1)
    assert WatermarkMode(window.mode_combo.currentData()) is WatermarkMode.REMOVE_IF_PRESENT
    assert window.process_button.text().startswith("检测并去除")
    window.close()
    app.processEvents()


def test_watermark_desktop_adds_only_supported_videos(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    video = tmp_path / "post.MP4"
    video.write_bytes(b"video")
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"image")
    window = MainWindow(state_root=tmp_path / "state", output_root=tmp_path / "out")

    window.add_files([video, image])

    assert window._paths == [video.resolve()]
    assert "post.MP4" in window.file_list.item(0).text()
    window.close()
    app.processEvents()


def test_watermark_desktop_locks_entire_form_while_processing(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    video = tmp_path / "post.mp4"
    video.write_bytes(b"video")
    window = MainWindow(state_root=tmp_path / "state", output_root=tmp_path / "out")
    window.add_files([video])
    window.mode_combo.setCurrentIndex(1)

    window._set_form_locked(True)

    assert window._form_locked
    for control in (
        window.choose_button,
        window.clear_button,
        window.select_region_button,
        window.file_list,
        window.output_path,
        window.output_button,
        window.mode_combo,
        window.confidence_combo,
        window.frames_display,
        window.repair_quality_combo,
        window.temporal_check,
        window.track_region_check,
        window.process_button,
    ):
        assert not control.isEnabled()

    another = tmp_path / "another.mp4"
    another.write_bytes(b"video")
    window.add_files([another])
    assert window._paths == [video.resolve()]

    window._set_form_locked(False)
    assert window.choose_button.isEnabled()
    assert window.file_list.isEnabled()
    assert window.process_button.isEnabled()
    window.close()
    app.processEvents()


def test_watermark_desktop_helpers(tmp_path: Path) -> None:
    assert format_bytes(1_048_576) == "1.0 MB"
    assert is_supported_video(tmp_path / "post.webm")
    assert not is_supported_video(tmp_path / "post.png")
