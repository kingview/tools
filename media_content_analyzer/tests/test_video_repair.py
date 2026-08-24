from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from media_content_analyzer.contracts import WatermarkRegion
from media_content_analyzer.errors import AnalyzerError, ErrorCode
from media_content_analyzer.video_repair import CommandVideoRepairBackend


def test_command_video_repair_backend_passes_json_contract(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    destination = tmp_path / "out" / "repaired.mp4"
    received: dict[str, object] = {}

    def runner(command, **kwargs):
        request_path = Path(command[command.index("--request") + 1])
        received.update(json.loads(request_path.read_text(encoding="utf-8")))
        Path(str(received["progress_path"])).write_text(
            '{"percent":63,"message":"AI 逐帧修复 63/100"}', encoding="utf-8"
        )
        time.sleep(0.03)
        Path(str(received["output_path"])).write_bytes(b"repaired")
        return subprocess.CompletedProcess(
            command,
            0,
            '{"ok":true,"worker":"lama-onnx-portable-v1","device":"coreml"}\n',
            "",
        )

    progress: list[tuple[int, str]] = []
    backend = CommandVideoRepairBackend(
        ["repair-worker"],
        runner=runner,
        progress_callback=lambda value, message: progress.append((value, message)),
        progress_poll_seconds=0.005,
    )
    backend.repair(
        source=source,
        destination=destination,
        regions=[WatermarkRegion(x=10, y=20, width=100, height=30)],
        moving=True,
    )

    assert destination.read_bytes() == b"repaired"
    assert received["schema_version"] == "1.2"
    assert received["moving"] is True
    assert received["regions"] == [
        {
            "x": 10,
            "y": 20,
            "width": 100,
            "height": 30,
            "confidence": 1.0,
            "first_seen_seconds": None,
            "last_seen_seconds": None,
            "tracked": True,
        }
    ]
    assert not list(destination.parent.glob(".repair-*.json"))
    assert backend.name == "lama-onnx-portable-v1:coreml"
    assert (63, "AI 逐帧修复 63/100") in progress
    assert not Path(str(received["progress_path"])).exists()


def test_command_video_repair_backend_reports_worker_failure(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    destination = tmp_path / "repaired.mp4"

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 9, "", "worker failed")

    backend = CommandVideoRepairBackend(["repair-worker"], runner=runner)
    with pytest.raises(AnalyzerError) as raised:
        backend.repair(
            source=source,
            destination=destination,
            regions=[WatermarkRegion(x=10, y=20, width=100, height=30)],
            moving=False,
        )

    assert raised.value.code is ErrorCode.WATERMARK_REMOVAL_FAILED
    assert "worker failed" in str(raised.value)
    assert not destination.exists()
