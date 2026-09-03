from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Sequence

import cv2
import imageio_ffmpeg
import numpy as np

from .contracts import WatermarkRegion
from .diagnostics import install_exception_hooks, record_exception
from .watermark_processor import (
    _blend_temporal_reference,
    _build_bidirectional_track,
    _mux_repaired_video,
    _normalize_regions,
    _static_fine_masks,
    _tracked_frame_mask,
    _tracking_templates,
)


DEFAULT_MODEL_REPO = "g-ronimo/lama"
DEFAULT_MODEL_FILE = "lama_512_fp16.onnx"
DEFAULT_MODEL_URL = (
    f"https://huggingface.co/{DEFAULT_MODEL_REPO}/resolve/main/{DEFAULT_MODEL_FILE}"
)
WORKER_NAME = "lama-onnx-portable-v1"


class ProgressReporter:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def update(self, percent: int, message: str, **details: object) -> None:
        if self.path is None:
            return
        payload = {
            "percent": max(0, min(100, int(percent))),
            "message": message,
            "updated_at": time.time(),
            **details,
        }
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            temporary.replace(self.path)
        except OSError:
            return
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class LamaOnnxInpainter:
    def __init__(
        self,
        *,
        model_path: Path | None = None,
        device: str = "auto",
        session_factory=None,
    ) -> None:
        import onnxruntime as ort

        self.model_path = model_path or resolve_model_path(download=True)
        providers, self.device = select_providers(device, ort.get_available_providers())
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        factory = session_factory or ort.InferenceSession
        self.session = factory(
            str(self.model_path),
            sess_options=options,
            providers=providers,
        )
        model_input = self.session.get_inputs()[0]
        self.input_name = model_input.name
        self.input_dtype = np.float16 if "float16" in model_input.type else np.float32

    def inpaint(self, frame: np.ndarray, mask: np.ndarray) -> np.ndarray:
        if not np.any(mask):
            return frame.copy()
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image_512, mask_512, geometry = _letterbox(rgb, mask, size=512)
        normalized_mask = (mask_512 > 0).astype(np.float32)
        normalized_image = image_512.astype(np.float32) / 255.0
        masked = normalized_image * (1.0 - normalized_mask[..., None])
        tensor = np.concatenate([masked, normalized_mask[..., None]], axis=2)
        tensor = np.transpose(tensor, (2, 0, 1))[None].astype(self.input_dtype)
        prediction = self.session.run(None, {self.input_name: tensor})[0]
        prediction = np.asarray(prediction[0], dtype=np.float32)
        prediction = np.transpose(prediction, (1, 2, 0))
        prediction = np.clip(prediction * 255.0, 0, 255).astype(np.uint8)
        prediction = _unletterbox(prediction, geometry, frame.shape[1], frame.shape[0])
        prediction = cv2.cvtColor(prediction, cv2.COLOR_RGB2BGR)
        feather = cv2.GaussianBlur(mask, (0, 0), sigmaX=2.0).astype(np.float32) / 255.0
        solid = (mask > 0).astype(np.float32)
        alpha = np.maximum(feather, solid)[..., None]
        return np.clip(
            frame.astype(np.float32) * (1.0 - alpha)
            + prediction.astype(np.float32) * alpha,
            0,
            255,
        ).astype(np.uint8)


def select_providers(
    requested: str,
    available: Sequence[str],
) -> tuple[list[str], str]:
    requested = requested.strip().lower()
    supported = set(available)
    if requested not in {"auto", "coreml", "mps", "cuda", "cpu"}:
        raise ValueError("VIDEO_REPAIR_DEVICE must be auto, coreml, mps, cuda, or cpu")
    if requested == "auto":
        if platform.system() == "Darwin" and "CoreMLExecutionProvider" in supported:
            requested = "coreml"
        elif "CUDAExecutionProvider" in supported:
            requested = "cuda"
        else:
            requested = "cpu"
    if requested in {"coreml", "mps"}:
        if "CoreMLExecutionProvider" not in supported:
            raise RuntimeError("CoreMLExecutionProvider is unavailable")
        return ["CoreMLExecutionProvider", "CPUExecutionProvider"], "coreml"
    if requested == "cuda":
        if "CUDAExecutionProvider" not in supported:
            raise RuntimeError(
                "CUDAExecutionProvider is unavailable; install onnxruntime-gpu"
            )
        return ["CUDAExecutionProvider", "CPUExecutionProvider"], "cuda"
    if "CPUExecutionProvider" not in supported:
        raise RuntimeError("CPUExecutionProvider is unavailable")
    return ["CPUExecutionProvider"], "cpu"


def resolve_model_path(*, download: bool) -> Path:
    configured = os.getenv("VIDEO_REPAIR_MODEL_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"configured model does not exist: {path}")
        return path
    bundled_root = getattr(sys, "_MEIPASS", None)
    if bundled_root:
        bundled_path = Path(bundled_root) / "models" / DEFAULT_MODEL_FILE
        if bundled_path.is_file():
            return bundled_path.resolve()
    cache_root = Path(
        os.getenv(
            "VIDEO_REPAIR_MODEL_DIR",
            str(Path.home() / ".cache" / "social-agent" / "video-repair"),
        )
    ).expanduser().resolve()
    local_path = cache_root / DEFAULT_MODEL_FILE
    if local_path.is_file():
        return local_path
    if not download:
        return local_path
    cache_root.mkdir(parents=True, exist_ok=True)
    temporary = local_path.with_suffix(f"{local_path.suffix}.download")
    temporary.unlink(missing_ok=True)
    try:
        with urllib.request.urlopen(DEFAULT_MODEL_URL, timeout=120) as response:
            with temporary.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
        if temporary.stat().st_size < 1_000_000:
            raise ValueError("downloaded repair model is unexpectedly small")
        temporary.replace(local_path)
    finally:
        temporary.unlink(missing_ok=True)
    return local_path


def repair_video(request: dict[str, object]) -> dict[str, object]:
    source = Path(str(request["input_path"])).expanduser().resolve(strict=True)
    destination = Path(str(request["output_path"])).expanduser().resolve()
    progress_path = _progress_path(request.get("progress_path"), destination)
    progress = ProgressReporter(progress_path)
    progress.update(1, "正在加载 AI 修复模型…")
    raw_regions = request.get("regions")
    if not isinstance(raw_regions, list) or not raw_regions:
        raise ValueError("request contains no repair regions")
    regions: list[WatermarkRegion] = []
    tracked_flags: list[bool] = []
    for raw_region in raw_regions:
        if not isinstance(raw_region, dict):
            raise ValueError("invalid repair region")
        values = dict(raw_region)
        tracked_flags.append(bool(values.pop("tracked", request.get("moving", False))))
        regions.append(WatermarkRegion.model_validate(values))

    device = os.getenv("VIDEO_REPAIR_DEVICE", "auto")
    model = LamaOnnxInpainter(device=device)
    progress.update(4, f"AI 模型已就绪 · {model.device.upper()}")
    _run_video_inpainting(
        source,
        destination,
        regions,
        tracked_flags,
        model,
        progress.update,
    )
    return {
        "ok": True,
        "worker": WORKER_NAME,
        "device": model.device,
        "model": str(model.model_path),
        "output_path": str(destination),
    }


def _run_video_inpainting(
    source: Path,
    destination: Path,
    regions: Sequence[WatermarkRegion],
    tracked_flags: Sequence[bool],
    model: LamaOnnxInpainter,
    progress: Callable[..., None] | None = None,
) -> None:
    report = progress or (lambda *args, **kwargs: None)
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise ValueError(f"cannot open video: {source.name}")
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0) or 25.0
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    ok, first_frame = capture.read()
    capture.release()
    if not ok or first_frame is None or frame_count <= 0:
        raise ValueError("video contains no readable frames")
    normalized = _normalize_regions(regions, width, height)
    if len(normalized) != len(regions) or len(tracked_flags) != len(regions):
        raise ValueError("repair regions are outside video bounds")

    report(6, "正在生成细粒度水印遮罩…")
    static_masks = _static_fine_masks(source, normalized)
    local_masks: list[np.ndarray] = []
    position_tracks: list[list[tuple[int, int, int, int]]] = []
    for region_index, (tracked, region, box, static_mask) in enumerate(zip(
        tracked_flags, regions, normalized, static_masks, strict=True
    ), start=1):
        report(
            7 + round(region_index / len(regions) * 8),
            f"正在跟踪水印区域 {region_index}/{len(regions)}…",
        )
        if not tracked:
            local_masks.append(np.asarray(static_mask, dtype=np.uint8))
            position_tracks.append([box] * frame_count)
            continue
        anchor_index = min(
            frame_count - 1,
            max(0, int(round((region.first_seen_seconds or 0.0) * fps))),
        )
        anchor_capture = cv2.VideoCapture(str(source))
        anchor_capture.set(cv2.CAP_PROP_POS_FRAMES, anchor_index)
        anchor_ok, anchor_frame = anchor_capture.read()
        anchor_capture.release()
        if not anchor_ok or anchor_frame is None:
            anchor_frame = first_frame
            anchor_index = 0
        templates, edge_modes = _tracking_templates(anchor_frame, [box])
        # The detector already returns a tight padded box for each moving
        # overlay component. High-quality inpainting must cover that complete
        # box: stroke-only masks can preserve bright interiors or reconstruct
        # recognizable letter fragments after temporal blending.
        local_masks.append(_solid_tracked_mask(box))
        position_tracks.append(
            _build_bidirectional_track(
                source,
                templates[0],
                edge_modes[0],
                box,
                anchor_index,
                frame_count,
                240,
            )
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.lama-video.mp4")
    writer = cv2.VideoWriter(
        str(temporary), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
    )
    if not writer.isOpened():
        raise ValueError("cannot create temporary repair video")
    capture = cv2.VideoCapture(str(source))
    previous_gray = None
    previous_repaired = None
    frame_index = 0
    inference_started = time.monotonic()
    report(15, f"开始 AI 逐帧修复 · 共 {frame_count} 帧")
    try:
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            mask = np.zeros((height, width), dtype=np.uint8)
            for tracked, positions, local_mask in zip(
                tracked_flags, position_tracks, local_masks, strict=True
            ):
                x, y, box_width, box_height = positions[
                    min(frame_index, len(positions) - 1)
                ]
                applied_box, applied_mask = (
                    _tracked_frame_mask(
                        frame,
                        (x, y, box_width, box_height),
                        local_mask,
                        width,
                        height,
                    )
                    if tracked
                    else ((x, y, box_width, box_height), local_mask)
                )
                mask_x, mask_y, mask_width, mask_height = applied_box
                mask[mask_y : mask_y + mask_height, mask_x : mask_x + mask_width] = cv2.max(
                    mask[mask_y : mask_y + mask_height, mask_x : mask_x + mask_width],
                    applied_mask,
                )
            repaired = model.inpaint(frame, mask)
            current_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if previous_gray is not None and previous_repaired is not None:
                repaired = _blend_temporal_reference(
                    previous_gray,
                    current_gray,
                    previous_repaired,
                    repaired,
                    mask,
                )
            writer.write(repaired)
            previous_gray = current_gray
            previous_repaired = repaired
            frame_index += 1
            update_every = max(1, int(round(fps / 2)))
            if frame_index == frame_count or frame_index % update_every == 0:
                elapsed = max(0.001, time.monotonic() - inference_started)
                frames_per_second = frame_index / elapsed
                eta_seconds = max(
                    0, round((frame_count - frame_index) / frames_per_second)
                )
                percent = 15 + round(frame_index / frame_count * 80)
                report(
                    percent,
                    f"AI 逐帧修复 {frame_index}/{frame_count} · "
                    f"预计剩余 {_format_duration(eta_seconds)}",
                    frame=frame_index,
                    total_frames=frame_count,
                    eta_seconds=eta_seconds,
                )
    finally:
        capture.release()
        writer.release()
    report(97, "画面修复完成，正在合并原始音频…")
    _mux_repaired_video(
        imageio_ffmpeg.get_ffmpeg_exe(),
        temporary,
        source,
        destination,
        subprocess.run,
    )
    report(100, "AI 高质量修复完成")


def _solid_tracked_mask(box: tuple[int, int, int, int]) -> np.ndarray:
    _, _, width, height = box
    return np.full((height, width), 255, dtype=np.uint8)


def _progress_path(value: object, destination: Path) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).expanduser().resolve()
    if path.parent != destination.parent:
        raise ValueError("progress_path must use the output directory")
    return path


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {remainder} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def _letterbox(
    image: np.ndarray, mask: np.ndarray, *, size: int
) -> tuple[np.ndarray, np.ndarray, tuple[int, int, int, int]]:
    height, width = image.shape[:2]
    scale = min(size / width, size / height)
    resized_width = max(1, int(round(width * scale)))
    resized_height = max(1, int(round(height * scale)))
    resized_image = cv2.resize(
        image, (resized_width, resized_height), interpolation=cv2.INTER_AREA
    )
    resized_mask = cv2.resize(
        mask, (resized_width, resized_height), interpolation=cv2.INTER_NEAREST
    )
    left = (size - resized_width) // 2
    right = size - resized_width - left
    top = (size - resized_height) // 2
    bottom = size - resized_height - top
    padded_image = cv2.copyMakeBorder(
        resized_image, top, bottom, left, right, cv2.BORDER_REFLECT_101
    )
    padded_mask = cv2.copyMakeBorder(
        resized_mask, top, bottom, left, right, cv2.BORDER_CONSTANT, value=0
    )
    return padded_image, padded_mask, (left, top, resized_width, resized_height)


def _unletterbox(
    image: np.ndarray,
    geometry: tuple[int, int, int, int],
    output_width: int,
    output_height: int,
) -> np.ndarray:
    left, top, width, height = geometry
    cropped = image[top : top + height, left : left + width]
    return cv2.resize(
        cropped, (output_width, output_height), interpolation=cv2.INTER_CUBIC
    )


def health_payload() -> dict[str, object]:
    try:
        import onnxruntime as ort

        providers = ort.get_available_providers()
        selected, device = select_providers(
            os.getenv("VIDEO_REPAIR_DEVICE", "auto"), providers
        )
        model_path = resolve_model_path(download=False)
        return {
            "ok": True,
            "worker": WORKER_NAME,
            "device": device,
            "selected_providers": selected,
            "available_providers": providers,
            "model_path": str(model_path),
            "model_ready": model_path.is_file(),
        }
    except Exception as exc:
        record_exception("media-content", "repair_worker.health", exc)
        return {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="video-repair-worker")
    parser.add_argument("--request", type=Path)
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--download-model", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    install_exception_hooks("media-content")
    args = build_parser().parse_args(argv)
    try:
        if args.health:
            payload = health_payload()
        elif args.download_model:
            payload = {"ok": True, "model_path": str(resolve_model_path(download=True))}
        elif args.request:
            request = json.loads(args.request.expanduser().resolve(strict=True).read_text("utf-8"))
            payload = repair_video(request)
        else:
            raise ValueError("one of --request, --health, or --download-model is required")
    except Exception as exc:
        record_exception("media-content", "repair_worker.execute", exc)
        payload = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
