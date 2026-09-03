from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np

from media_content_analyzer.video_repair_worker import (
    DEFAULT_MODEL_FILE,
    LamaOnnxInpainter,
    ProgressReporter,
    _format_duration,
    _letterbox,
    _solid_tracked_mask,
    _unletterbox,
    resolve_model_path,
    select_providers,
)


def test_provider_auto_prefers_coreml_on_mac(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")

    providers, device = select_providers(
        "auto", ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    )

    assert device == "coreml"
    assert providers[0] == "CoreMLExecutionProvider"


def test_provider_auto_prefers_cuda_off_mac(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")

    providers, device = select_providers(
        "auto", ["CUDAExecutionProvider", "CPUExecutionProvider"]
    )

    assert device == "cuda"
    assert providers[0] == "CUDAExecutionProvider"


def test_letterbox_round_trip_preserves_dimensions() -> None:
    image = np.zeros((240, 640, 3), dtype=np.uint8)
    mask = np.zeros((240, 640), dtype=np.uint8)
    mask[80:120, 250:390] = 255

    boxed, boxed_mask, geometry = _letterbox(image, mask, size=512)
    restored = _unletterbox(boxed, geometry, 640, 240)

    assert boxed.shape == (512, 512, 3)
    assert boxed_mask.shape == (512, 512)
    assert restored.shape == image.shape
    assert np.count_nonzero(boxed_mask) > 0


def test_lama_inpainter_only_composites_masked_pixels(tmp_path: Path) -> None:
    class Input:
        name = "input"
        type = "tensor(float16)"

    class Session:
        def get_inputs(self):
            return [Input()]

        def run(self, output_names, inputs):
            return [np.full((1, 3, 512, 512), 0.5, dtype=np.float16)]

    def factory(*args, **kwargs):
        return Session()

    model_path = tmp_path / "lama.onnx"
    model_path.write_bytes(b"fake")
    inpainter = LamaOnnxInpainter(
        model_path=model_path,
        device="cpu",
        session_factory=factory,
    )
    frame = np.zeros((64, 96, 3), dtype=np.uint8)
    frame[:] = (10, 20, 30)
    mask = np.zeros((64, 96), dtype=np.uint8)
    mask[20:40, 30:60] = 255

    repaired = inpainter.inpaint(frame, mask)

    assert np.array_equal(repaired[0, 0], frame[0, 0])
    assert not np.array_equal(repaired[30, 45], frame[30, 45])


def test_high_quality_tracked_mask_covers_complete_overlay_box() -> None:
    mask = _solid_tracked_mask((85, 1222, 107, 29))

    assert mask.shape == (29, 107)
    assert np.all(mask == 255)


def test_model_downloader_writes_cache_atomically(tmp_path: Path, monkeypatch) -> None:
    model_bytes = b"model" * 250_001
    monkeypatch.setenv("VIDEO_REPAIR_MODEL_DIR", str(tmp_path))
    monkeypatch.delenv("VIDEO_REPAIR_MODEL_PATH", raising=False)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: io.BytesIO(model_bytes),
    )

    path = resolve_model_path(download=True)

    assert path == tmp_path / DEFAULT_MODEL_FILE
    assert path.read_bytes() == model_bytes
    assert not path.with_suffix(f"{path.suffix}.download").exists()


def test_bundled_model_is_preferred_without_external_cache(
    tmp_path: Path, monkeypatch
) -> None:
    bundled = tmp_path / "models" / DEFAULT_MODEL_FILE
    bundled.parent.mkdir()
    bundled.write_bytes(b"bundled-model")
    monkeypatch.delenv("VIDEO_REPAIR_MODEL_PATH", raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)

    path = resolve_model_path(download=False)

    assert path == bundled.resolve()


def test_progress_reporter_writes_machine_readable_status(tmp_path: Path) -> None:
    path = tmp_path / "progress.json"

    ProgressReporter(path).update(
        42,
        "AI 逐帧修复 42/100",
        frame=42,
        total_frames=100,
        eta_seconds=58,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["percent"] == 42
    assert payload["frame"] == 42
    assert payload["eta_seconds"] == 58
    assert _format_duration(58) == "58 秒"
    assert _format_duration(125) == "2 分 5 秒"
