from __future__ import annotations

import hashlib
import mimetypes
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import imageio_ffmpeg

from .contracts import (
    ArtifactRef,
    ProcessWatermarkInput,
    ProcessWatermarkOutput,
    ProcessedWatermarkArtifact,
    WatermarkArtifactResult,
    WatermarkKind,
    WatermarkMode,
    WatermarkRepairQuality,
    WatermarkRegion,
)
from .errors import AnalyzerError, ErrorCode
from .video_repair import HighQualityVideoRepairBackend


@dataclass(frozen=True, slots=True)
class VideoSamples:
    frames: list[object]
    width: int
    height: int
    duration_seconds: float


@dataclass(slots=True)
class _DynamicCandidate:
    frame_index: int
    x: int
    y: int
    width: int
    height: int
    descriptor: object
    kind: str = "edge"


@dataclass(slots=True)
class _DynamicTrack:
    candidates: list[_DynamicCandidate]
    descriptor: object
    similarity_sum: float = 0.0


class OpenCvWatermarkBackend:
    detector_version = "opencv-overlay-v6"

    def __init__(
        self,
        *,
        ffmpeg_path: str | None = None,
        high_quality_backend: HighQualityVideoRepairBackend | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._ffmpeg = ffmpeg_path or _find_ffmpeg()
        self._runner = runner
        self._high_quality_backend = high_quality_backend

    def process(
        self,
        request: ProcessWatermarkInput,
        artifacts: Sequence[Path],
        output_directory: Path,
    ) -> ProcessWatermarkOutput:
        results: list[WatermarkArtifactResult] = []
        for manifest, path in zip(request.artifacts, artifacts, strict=True):
            results.append(self._process_one(request, manifest, path, output_directory))
        processed_count = sum(item.processed_artifact is not None for item in results)
        return ProcessWatermarkOutput(
            items=results,
            detected_count=sum(item.detected for item in results),
            processed_count=processed_count,
            output_directory=str(output_directory) if processed_count else None,
            detector_version=self.detector_version,
        )

    def _process_one(
        self,
        request: ProcessWatermarkInput,
        manifest: ArtifactRef,
        path: Path,
        output_directory: Path,
    ) -> WatermarkArtifactResult:
        if not _is_video(manifest, path):
            return WatermarkArtifactResult(
                original=manifest,
                detected=False,
                needs_human_review=True,
                warnings=["当前版本只处理视频水印；该 artifact 已跳过。"],
            )

        manual = request.manual_regions.get(manifest.sha256) or request.manual_regions.get(
            str(path)
        )
        try:
            samples = _sample_video(path, request.sample_frames)
            dynamic_detected = False
            dynamic_region_ids: set[int] = set()
            if manual:
                regions = list(manual)
                if request.track_manual_regions:
                    dynamic_region_ids = {id(item) for item in regions}
            else:
                static_regions = _detect_static_regions(samples)
                dynamic_regions = _detect_dynamic_regions(samples)
                dynamic_detected = bool(dynamic_regions)
                regions = _merge_detected_regions(dynamic_regions, static_regions)
                dynamic_region_ids = {id(item) for item in dynamic_regions}
        except AnalyzerError:
            raise
        except Exception as exc:
            raise AnalyzerError(
                ErrorCode.WATERMARK_DETECTION_FAILED,
                f"watermark detection failed for {path.name}",
            ) from exc

        confidence = max((region.confidence for region in regions), default=0.0)
        detected = bool(regions)
        frame_area = max(1, samples.width * samples.height)
        manually_confirmed = bool(manual)
        qualified = (
            list(regions)
            if manually_confirmed
            else [
                region
                for region in regions
                if region.confidence >= request.minimum_confidence
                and region.width * region.height / frame_area <= 0.08
            ]
        )
        tracked_qualified = [id(region) in dynamic_region_ids for region in qualified]
        warnings: list[str] = []
        needs_review = detected and len(qualified) != len(regions)
        if needs_review:
            warnings.append("部分疑似区域置信度不足或面积过大，已跳过并建议人工复核。")
        processed = None
        quality_score = None
        repair_quality_applied = None
        repair_method = None

        if request.mode is WatermarkMode.REMOVE_IF_PRESENT and qualified:
            if not self._ffmpeg:
                raise AnalyzerError(
                    ErrorCode.CONFIGURATION_ERROR,
                    "FFmpeg is required for watermark removal",
                )
            output_path = output_directory / f"{path.stem}.watermark-removed.mp4"
            moving = any(tracked_qualified)
            repair_quality_applied = request.repair_quality
            if repair_quality_applied is WatermarkRepairQuality.AUTO:
                changed_ratio = sum(
                    region.width * region.height for region in qualified
                ) / frame_area
                repair_quality_applied = (
                    WatermarkRepairQuality.HIGH
                    if self._high_quality_backend is not None
                    and (moving or changed_ratio >= 0.015)
                    else WatermarkRepairQuality.BALANCED
                )
            if (
                repair_quality_applied is WatermarkRepairQuality.HIGH
                and self._high_quality_backend is None
            ):
                repair_quality_applied = WatermarkRepairQuality.BALANCED
                warnings.append("未找到高质量修复 Worker，已回退到本机时序修复。")
            try:
                if repair_quality_applied is WatermarkRepairQuality.HIGH:
                    assert self._high_quality_backend is not None
                    self._high_quality_backend.repair(
                        source=path,
                        destination=output_path,
                        regions=qualified,
                        moving=moving,
                        tracked_regions=tracked_qualified,
                    )
                    repair_method = self._high_quality_backend.name
                elif repair_quality_applied is WatermarkRepairQuality.BALANCED:
                    _remove_temporal_regions(
                        self._ffmpeg,
                        path,
                        output_path,
                        qualified,
                        track_flags=tracked_qualified,
                        search_radius=request.tracking_search_radius,
                        inpaint_radius=request.inpaint_radius,
                        temporal_consistency=request.temporal_consistency,
                        runner=self._runner,
                    )
                    repair_method = "opencv-fine-mask-temporal-v1"
                    if moving:
                        warnings.append("已自动跟踪动态水印并进行时序一致性修复。")
                        needs_review = True
                elif moving:
                    _remove_tracked_regions(
                        self._ffmpeg,
                        path,
                        output_path,
                        qualified,
                        request.tracking_search_radius,
                        request.inpaint_radius,
                        self._runner,
                    )
                    repair_method = "opencv-rectangle-inpaint-v1"
                    warnings.append(
                        "已逐帧跟踪动态水印并进行修复；建议检查完整输出。"
                        if dynamic_detected
                        else "已按人工框选区域逐帧跟踪动态水印；建议检查完整输出。"
                    )
                    needs_review = True
                else:
                    _remove_static_regions(
                        self._ffmpeg,
                        path,
                        output_path,
                        qualified,
                        samples.width,
                        samples.height,
                        self._runner,
                    )
                    repair_method = "ffmpeg-delogo-v1"
            except AnalyzerError:
                raise
            except Exception as exc:
                raise AnalyzerError(
                    ErrorCode.WATERMARK_REMOVAL_FAILED,
                    f"watermark removal failed for {path.name}",
                ) from exc
            processed = ProcessedWatermarkArtifact(
                path=str(output_path.resolve()),
                size_bytes=output_path.stat().st_size,
                sha256=_sha256(output_path),
                media_type=mimetypes.guess_type(output_path.name)[0] or "video/mp4",
                derived_from_sha256=manifest.sha256,
            )
            changed_area = sum(region.width * region.height for region in qualified)
            quality_score = max(0.5, min(0.99, 0.98 - changed_area / frame_area * 2.0))
            if quality_score < 0.75:
                needs_review = True
                warnings.append("修复区域较大，建议人工检查输出画质。")

        return WatermarkArtifactResult(
            original=manifest,
            detected=detected,
            kind=(
                WatermarkKind.MOVING
                if detected
                and (dynamic_detected or (manually_confirmed and request.track_manual_regions))
                else WatermarkKind.STATIC if detected else None
            ),
            confidence=confidence,
            regions=regions,
            processed_artifact=processed,
            quality_score=quality_score,
            repair_quality_requested=request.repair_quality,
            repair_quality_applied=repair_quality_applied,
            repair_method=repair_method,
            needs_human_review=needs_review,
            warnings=warnings,
        )


def _automatic_sample_frame_count(
    duration_seconds: float,
    frame_count: int,
) -> int:
    """Choose a duration-aware detection sample without oversampling long media."""

    import math

    desired = max(36, math.ceil(max(0.0, duration_seconds) * 2.0))
    desired = min(120, desired)
    return max(4, min(desired, frame_count)) if frame_count > 0 else desired


def _sample_video(path: Path, count: int | None) -> VideoSamples:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise AnalyzerError(
            ErrorCode.CONFIGURATION_ERROR,
            "OpenCV is required; install the 'image' extra",
        ) from exc

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise AnalyzerError(ErrorCode.UNSUPPORTED_MEDIA, f"cannot open video: {path.name}")
    try:
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        if width <= 0 or height <= 0:
            raise AnalyzerError(ErrorCode.UNSUPPORTED_MEDIA, "video dimensions are unavailable")
        duration = frame_count / fps if frame_count > 0 and fps > 0 else 0.0
        selected_count = (
            _automatic_sample_frame_count(duration, frame_count)
            if count is None
            else count
        )
        positions = (
            np.linspace(
                0,
                max(frame_count - 1, 0),
                min(selected_count, max(frame_count, 1)),
            ).astype(int)
            if frame_count > 0
            else np.arange(selected_count) * 15
        )
        frames: list[object] = []
        for position in positions:
            capture.set(cv2.CAP_PROP_POS_FRAMES, int(position))
            ok, frame = capture.read()
            if ok and frame is not None:
                frames.append(frame)
        if len(frames) < 4:
            raise AnalyzerError(
                ErrorCode.UNSUPPORTED_MEDIA,
                "not enough readable frames for watermark detection",
            )
        return VideoSamples(frames=frames, width=width, height=height, duration_seconds=duration)
    finally:
        capture.release()


def _detect_static_regions(samples: VideoSamples) -> list[WatermarkRegion]:
    import cv2
    import numpy as np

    target_width = min(720, samples.width)
    scale = target_width / samples.width
    target_height = max(1, int(samples.height * scale))
    edge_layers = []
    for frame in samples.frames:
        resized = cv2.resize(frame, (target_width, target_height), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        edge_layers.append(cv2.Canny(gray, 80, 180) > 0)
    persistence = np.mean(np.stack(edge_layers, axis=0), axis=0)
    stable = (persistence >= 0.68).astype(np.uint8) * 255

    horizontal = max(5, int(target_width * 0.025))
    vertical = max(3, int(target_height * 0.008))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (horizontal, vertical))
    grouped = cv2.morphologyEx(stable, cv2.MORPH_CLOSE, kernel, iterations=2)
    grouped = cv2.dilate(grouped, np.ones((3, 3), np.uint8), iterations=1)

    count, _, stats, _ = cv2.connectedComponentsWithStats(grouped, connectivity=8)
    candidates: list[WatermarkRegion] = []
    total_area = target_width * target_height
    for index in range(1, count):
        x, y, width, height, area = [int(value) for value in stats[index]]
        box_area = width * height
        if width < 12 or height < 6 or box_area < total_area * 0.00015:
            continue
        if box_area > total_area * 0.14 or area < 10:
            continue
        # Exact frame-edge components are usually crop/encoding boundaries, not
        # overlays. Real watermarks normally retain at least a tiny inset.
        if x <= 1 or y <= 1 or x + width >= target_width - 1 or y + height >= target_height - 1:
            continue
        persistent_density = float(np.mean(persistence[y : y + height, x : x + width]))
        stable_ratio = float(
            np.mean(persistence[y : y + height, x : x + width] >= 0.68)
        )
        # Overlay text has many edge pixels that remain in the same coordinates
        # across changing frames. Requiring that ratio avoids treating ordinary
        # scene detail as a watermark while allowing candidates anywhere.
        confidence = min(
            0.98,
            0.58
            + stable_ratio * 1.5
            + persistent_density * 0.3
            + _static_sample_support_bonus(len(samples.frames)),
        )
        if confidence < 0.62:
            continue
        vertical_padding = max(3, int(height * 0.28))
        # Low-opacity first/last glyphs can become disconnected after a video
        # has already been transcoded once. Keep horizontal text context so a
        # strong middle component still carries those weak edge glyphs into
        # the finer 50%-persistence repair mask.
        horizontal_padding = max(vertical_padding, int(height * 0.55))
        left = max(0, x - horizontal_padding)
        top = max(0, y - vertical_padding)
        right = min(target_width, x + width + horizontal_padding)
        bottom = min(target_height, y + height + vertical_padding)
        candidates.append(
            WatermarkRegion(
                x=int(left / scale),
                y=int(top / scale),
                width=max(3, int((right - left) / scale)),
                height=max(3, int((bottom - top) / scale)),
                confidence=confidence,
                first_seen_seconds=0.0,
                last_seen_seconds=samples.duration_seconds or None,
            )
        )
    candidates.sort(key=lambda item: item.confidence, reverse=True)
    return candidates[:4]


def _static_sample_support_bonus(sample_count: int) -> float:
    """Calibrate confidence when a fixed candidate survives denser sampling.

    The persistence estimate becomes more trustworthy as independent samples
    increase. Without this small bounded bonus, the same translucent overlay
    can fall just below the UI threshold when automatic sampling replaces the
    former 18-frame default.
    """

    return min(0.02, max(0, sample_count - 18) / 39 * 0.02)


def _merge_detected_regions(
    dynamic_regions: Sequence[WatermarkRegion],
    static_regions: Sequence[WatermarkRegion],
) -> list[WatermarkRegion]:
    """Keep simultaneous moving and static overlays while removing duplicates."""

    output: list[WatermarkRegion] = []
    for region in [*dynamic_regions, *static_regions]:
        if any(_region_iou(region, existing) >= 0.45 for existing in output):
            continue
        output.append(region)
    return output[:6]


def _region_iou(left: WatermarkRegion, right: WatermarkRegion) -> float:
    intersection_left = max(left.x, right.x)
    intersection_top = max(left.y, right.y)
    intersection_right = min(left.x + left.width, right.x + right.width)
    intersection_bottom = min(left.y + left.height, right.y + right.height)
    intersection = max(0, intersection_right - intersection_left) * max(
        0, intersection_bottom - intersection_top
    )
    union = left.width * left.height + right.width * right.height - intersection
    return intersection / max(1, union)


def _detect_dynamic_regions(samples: VideoSamples) -> list[WatermarkRegion]:
    """Find a recurring overlay shape whose coordinates change across frames.

    Candidate patches are reduced to normalized edge descriptors, so the same
    text/logo can be associated even while the underlying scene changes.
    Tracks may start after the first sampled frame so cyclic scrolling and
    intermittent overlays can still be detected.
    """

    import cv2
    import numpy as np

    target_width = min(720, samples.width)
    scale = target_width / samples.width
    target_height = max(1, int(samples.height * scale))
    per_frame: list[list[_DynamicCandidate]] = []
    for frame_index, frame in enumerate(samples.frames):
        resized = cv2.resize(
            frame, (target_width, target_height), interpolation=cv2.INTER_AREA
        )
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 90, 190)
        horizontal = max(7, int(target_width * 0.018))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (horizontal, 3))
        grouped = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=1)
        grouped = cv2.dilate(grouped, np.ones((2, 2), np.uint8), iterations=1)
        count, _, stats, _ = cv2.connectedComponentsWithStats(grouped, connectivity=8)
        frame_candidates: list[_DynamicCandidate] = []
        total_area = target_width * target_height
        for index in range(1, count):
            x, y, width, height, area = [int(value) for value in stats[index]]
            box_area = width * height
            aspect = width / max(1, height)
            if width < max(18, target_width * 0.025) or height < 7:
                continue
            if width > target_width * 0.60 or height > target_height * 0.25:
                continue
            if box_area < total_area * 0.00018 or box_area > total_area * 0.10:
                continue
            if x <= 1 or y <= 1 or x + width >= target_width - 1 or y + height >= target_height - 1:
                continue
            if not 0.65 <= aspect <= 18.0 or area / box_area < 0.10:
                continue
            descriptor = _edge_descriptor(edges[y : y + height, x : x + width])
            if descriptor is None:
                continue
            frame_candidates.append(
                _DynamicCandidate(
                    frame_index=frame_index,
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    descriptor=descriptor,
                )
            )
        # Platform exports often move a compound watermark between corners:
        # a light platform badge, account text, and a circular account avatar.
        # Edge grouping finds the text but needs shape-aware candidates for the
        # filled badge and square avatar so all components reach the repairer.
        for candidate in [
            *_corner_badge_candidates(
                frame_index, resized, gray, edges, target_width, target_height
            ),
            *_corner_avatar_candidates(
                frame_index, resized, gray, edges, target_width, target_height
            ),
        ]:
            # Prefer the shape-aware candidate over an overlapping generic
            # edge group. Besides keeping the full filled area, this prevents
            # an identical badge from being split into unrelated track kinds.
            frame_candidates = [
                existing
                for existing in frame_candidates
                if _candidate_iou(candidate, existing) < 0.55
            ]
            frame_candidates.append(candidate)
        per_frame.append(frame_candidates)

    tracks: list[_DynamicTrack] = []
    for frame_index, frame_candidates in enumerate(per_frame):
        used_tracks: set[int] = set()
        for candidate in frame_candidates:
            best_track_index = -1
            best_similarity = 0.0
            for track_index, track in enumerate(tracks):
                if track_index in used_tracks or track.candidates[-1].frame_index == frame_index:
                    continue
                previous = track.candidates[-1]
                if candidate.kind != previous.kind:
                    continue
                aspect_ratio = (candidate.width / candidate.height) / (
                    previous.width / previous.height
                )
                area_ratio = (candidate.width * candidate.height) / max(
                    1, previous.width * previous.height
                )
                if not 0.65 <= aspect_ratio <= 1.55 or not 0.45 <= area_ratio <= 2.2:
                    continue
                similarity = float(np.dot(track.descriptor, candidate.descriptor))
                if similarity > best_similarity:
                    best_track_index = track_index
                    best_similarity = similarity
            if best_track_index >= 0 and best_similarity >= 0.68:
                track = tracks[best_track_index]
                track.candidates.append(candidate)
                track.similarity_sum += best_similarity
                prototype = track.descriptor * 0.75 + candidate.descriptor * 0.25
                track.descriptor = prototype / (np.linalg.norm(prototype) + 1e-6)
                used_tracks.add(best_track_index)
            else:
                tracks.append(_DynamicTrack([candidate], candidate.descriptor.copy()))
                used_tracks.add(len(tracks) - 1)

    minimum_occurrences = max(4, int(round(len(samples.frames) * 0.35)))
    output: list[WatermarkRegion] = []
    for track in tracks:
        if len(track.candidates) < minimum_occurrences:
            continue
        centers_x = np.array(
            [item.x + item.width / 2 for item in track.candidates], dtype=np.float32
        )
        centers_y = np.array(
            [item.y + item.height / 2 for item in track.candidates], dtype=np.float32
        )
        mean_width = float(np.mean([item.width for item in track.candidates]))
        mean_height = float(np.mean([item.height for item in track.candidates]))
        movement = max(float(np.ptp(centers_x)), float(np.ptp(centers_y)))
        if movement < max(8.0, min(mean_width, mean_height) * 0.35):
            continue
        occurrence_ratio = len(track.candidates) / len(samples.frames)
        mean_similarity = track.similarity_sum / max(1, len(track.candidates) - 1)
        confidence = min(0.96, 0.48 + occurrence_ratio * 0.35 + mean_similarity * 0.32)
        if confidence < 0.84:
            continue
        first = track.candidates[0]
        padding = max(3, int(min(first.width, first.height) * 0.10))
        left = max(0, first.x - padding)
        top = max(0, first.y - padding)
        right = min(target_width, first.x + first.width + padding)
        bottom = min(target_height, first.y + first.height + padding)
        output.append(
            WatermarkRegion(
                x=int(left / scale),
                y=int(top / scale),
                width=max(3, int((right - left) / scale)),
                height=max(3, int((bottom - top) / scale)),
                confidence=confidence,
                first_seen_seconds=(
                    samples.duration_seconds
                    * first.frame_index
                    / max(1, len(samples.frames) - 1)
                ),
                last_seen_seconds=samples.duration_seconds or None,
            )
        )
    output.sort(key=lambda item: item.confidence, reverse=True)
    return output[:6]


def _corner_badge_candidates(
    frame_index: int,
    frame: object,
    gray: object,
    edges: object,
    width: int,
    height: int,
) -> list[_DynamicCandidate]:
    """Detect persistent light platform pills/logos near a frame corner."""

    import cv2
    import numpy as np

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    light = ((gray >= 220) & (hsv[:, :, 1] <= 70)).astype(np.uint8) * 255
    kernel_size = max(5, int(round(width * 0.012)))
    if kernel_size % 2 == 0:
        kernel_size += 1
    light = cv2.morphologyEx(
        light,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)),
        iterations=2,
    )
    count, _, stats, _ = cv2.connectedComponentsWithStats(light, connectivity=8)
    output: list[_DynamicCandidate] = []
    for index in range(1, count):
        x, y, box_width, box_height, area = [int(value) for value in stats[index]]
        center_x = x + box_width / 2
        center_y = y + box_height / 2
        in_corner = (
            (center_x <= width * 0.28 or center_x >= width * 0.72)
            and (center_y <= height * 0.18 or center_y >= height * 0.82)
        )
        aspect = box_width / max(1, box_height)
        fill_ratio = area / max(1, box_width * box_height)
        if not in_corner:
            continue
        if not max(18, width * 0.025) <= box_width <= width * 0.28:
            continue
        if not max(8, height * 0.006) <= box_height <= max(
            height * 0.10, width * 0.08
        ):
            continue
        if not 0.7 <= aspect <= 8.0 or fill_ratio < 0.42:
            continue
        descriptor = _edge_descriptor(
            edges[y : y + box_height, x : x + box_width]
        )
        if descriptor is None:
            continue
        output.append(
            _DynamicCandidate(
                frame_index=frame_index,
                x=x,
                y=y,
                width=box_width,
                height=box_height,
                descriptor=descriptor,
                kind="badge",
            )
        )
    return output


def _corner_avatar_candidates(
    frame_index: int,
    frame: object,
    gray: object,
    edges: object,
    width: int,
    height: int,
) -> list[_DynamicCandidate]:
    """Detect a repeated, colorful circular avatar that jumps between corners."""

    import cv2
    import numpy as np

    blurred = cv2.GaussianBlur(gray, (5, 5), 1.2)
    maximum_radius = max(16, int(round(min(width, height) * 0.075)))
    circles = cv2.HoughCircles(
        blurred,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=max(24, int(round(min(width, height) * 0.035))),
        param1=110,
        param2=28,
        minRadius=max(10, int(round(width * 0.026))),
        maxRadius=maximum_radius,
    )
    if circles is None:
        return []
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    yy, xx = np.ogrid[:height, :width]
    output: list[_DynamicCandidate] = []
    for raw_x, raw_y, raw_radius in circles[0]:
        center_x = int(round(float(raw_x)))
        center_y = int(round(float(raw_y)))
        radius = int(round(float(raw_radius)))
        in_corner = (
            (center_x <= width * 0.20 or center_x >= width * 0.80)
            and (center_y <= height * 0.15 or center_y >= height * 0.85)
        )
        if not in_corner:
            continue
        circle_mask = (xx - center_x) ** 2 + (yy - center_y) ** 2 <= radius**2
        saturation = hsv[:, :, 1][circle_mask]
        if saturation.size == 0:
            continue
        if float(np.mean(saturation)) < 45.0 or float(np.mean(saturation >= 60)) < 0.25:
            continue
        padded_radius = max(radius + 3, int(round(radius * 1.30)))
        left = max(0, center_x - padded_radius)
        top = max(0, center_y - padded_radius)
        right = min(width, center_x + padded_radius + 1)
        bottom = min(height, center_y + padded_radius + 1)
        # A fully visible circular avatar produces a stable template. Large
        # scene circles clipped by the frame edge are common false positives
        # around people and furniture, and cannot be tracked reliably.
        if (
            center_x - padded_radius < 0
            or center_y - padded_radius < 0
            or center_x + padded_radius >= width
            or center_y + padded_radius >= height
        ):
            continue
        descriptor = _edge_descriptor(edges[top:bottom, left:right])
        if descriptor is None:
            continue
        output.append(
            _DynamicCandidate(
                frame_index=frame_index,
                x=left,
                y=top,
                width=right - left,
                height=bottom - top,
                descriptor=descriptor,
                kind="avatar",
            )
        )
    return output


def _candidate_iou(left: _DynamicCandidate, right: _DynamicCandidate) -> float:
    intersection_left = max(left.x, right.x)
    intersection_top = max(left.y, right.y)
    intersection_right = min(left.x + left.width, right.x + right.width)
    intersection_bottom = min(left.y + left.height, right.y + right.height)
    intersection = max(0, intersection_right - intersection_left) * max(
        0, intersection_bottom - intersection_top
    )
    union = left.width * left.height + right.width * right.height - intersection
    return intersection / max(1, union)


def _edge_descriptor(edges: object) -> object | None:
    import cv2
    import numpy as np

    height, width = edges.shape  # type: ignore[attr-defined]
    if width < 3 or height < 3:
        return None
    canvas = np.zeros((48, 160), dtype=np.uint8)
    scale = min(156 / width, 44 / height)
    resized_width = max(1, int(width * scale))
    resized_height = max(1, int(height * scale))
    resized = cv2.resize(
        edges, (resized_width, resized_height), interpolation=cv2.INTER_AREA
    )
    resized = cv2.dilate(resized, np.ones((2, 2), np.uint8), iterations=1)
    top = (48 - resized_height) // 2
    left = (160 - resized_width) // 2
    canvas[top : top + resized_height, left : left + resized_width] = resized
    descriptor = (canvas > 0).astype(np.float32).reshape(-1)
    norm = float(np.linalg.norm(descriptor))
    if norm < 4.0:
        return None
    return descriptor / norm


def _remove_static_regions(
    ffmpeg: str,
    source: Path,
    destination: Path,
    regions: Sequence[WatermarkRegion],
    frame_width: int,
    frame_height: int,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    filters = []
    for region in regions:
        x = min(max(1, region.x), max(1, frame_width - 4))
        y = min(max(1, region.y), max(1, frame_height - 4))
        width = min(region.width, frame_width - x - 1)
        height = min(region.height, frame_height - y - 1)
        if width >= 3 and height >= 3:
            filters.append(f"delogo=x={x}:y={y}:w={width}:h={height}:show=0")
    if not filters:
        raise AnalyzerError(ErrorCode.WATERMARK_REMOVAL_FAILED, "no valid regions to remove")
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-vf",
        ",".join(filters),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    completed = runner(command, capture_output=True, text=True, timeout=3_600, check=False)
    if completed.returncode != 0 or not destination.is_file():
        destination.unlink(missing_ok=True)
        detail = (completed.stderr or "FFmpeg did not create an output file").strip()[-1_000:]
        raise AnalyzerError(ErrorCode.WATERMARK_REMOVAL_FAILED, detail)


def _remove_temporal_regions(
    ffmpeg: str,
    source: Path,
    destination: Path,
    regions: Sequence[WatermarkRegion],
    *,
    track_flags: Sequence[bool],
    search_radius: int,
    inpaint_radius: int,
    temporal_consistency: bool,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Repair only overlay strokes and stabilize repaired pixels over time."""

    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise AnalyzerError(
            ErrorCode.CONFIGURATION_ERROR,
            "OpenCV is required for temporal watermark repair",
        ) from exc

    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise AnalyzerError(ErrorCode.UNSUPPORTED_MEDIA, f"cannot open video: {source.name}")
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0) or 25.0
    ok, first_frame = capture.read()
    if not ok or first_frame is None:
        capture.release()
        raise AnalyzerError(ErrorCode.UNSUPPORTED_MEDIA, "cannot read first video frame")

    normalized = _normalize_regions(regions, width, height)
    if not normalized:
        capture.release()
        raise AnalyzerError(ErrorCode.WATERMARK_REMOVAL_FAILED, "no valid repair regions")
    if len(track_flags) != len(normalized):
        capture.release()
        raise AnalyzerError(
            ErrorCode.WATERMARK_REMOVAL_FAILED,
            "repair-region tracking metadata is inconsistent",
        )
    static_masks = _static_fine_masks(source, normalized)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if frame_count <= 0:
        capture.release()
        raise AnalyzerError(ErrorCode.UNSUPPORTED_MEDIA, "video frame count is unavailable")
    position_tracks: list[list[tuple[int, int, int, int]]] = []
    local_masks: list[object] = []
    for tracked, region, normalized_region, static_mask in zip(
        track_flags, regions, normalized, static_masks, strict=True
    ):
        if not tracked:
            position_tracks.append([normalized_region] * frame_count)
            local_masks.append(static_mask)
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
        templates, template_uses_edges = _tracking_templates(
            anchor_frame, [normalized_region]
        )
        local_masks.append(
            _dynamic_fine_masks(anchor_frame, [normalized_region])[0]
        )
        position_tracks.append(
            _build_bidirectional_track(
                source,
                templates[0],
                template_uses_edges[0],
                normalized_region,
                anchor_index,
                frame_count,
                search_radius,
            )
        )

    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
    ok, first_frame = capture.read()
    if not ok or first_frame is None:
        capture.release()
        raise AnalyzerError(ErrorCode.UNSUPPORTED_MEDIA, "cannot restart video decoding")

    temporary = destination.with_name(f".{destination.stem}.temporal-video.mp4")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(temporary),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        capture.release()
        raise AnalyzerError(ErrorCode.WATERMARK_REMOVAL_FAILED, "cannot create repair video")

    previous_gray = None
    previous_repaired = None
    frame_index = 0
    try:
        frame = first_frame
        while frame is not None:
            gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            mask = np.zeros((height, width), dtype=np.uint8)
            for tracked, position_track, local_mask in zip(
                track_flags,
                position_tracks,
                local_masks,
                strict=True,
            ):
                x, y, box_width, box_height = position_track[
                    min(frame_index, len(position_track) - 1)
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
            repaired = cv2.inpaint(frame, mask, float(inpaint_radius), cv2.INPAINT_TELEA)
            if temporal_consistency and previous_gray is not None and previous_repaired is not None:
                repaired = _blend_temporal_reference(
                    previous_gray,
                    gray_frame,
                    previous_repaired,
                    repaired,
                    mask,
                )
            writer.write(repaired)
            previous_gray = gray_frame
            previous_repaired = repaired
            frame_index += 1
            ok, frame = capture.read()
            if not ok:
                frame = None
    finally:
        writer.release()
        capture.release()

    _mux_repaired_video(ffmpeg, temporary, source, destination, runner)


def _normalize_regions(
    regions: Sequence[WatermarkRegion], width: int, height: int
) -> list[tuple[int, int, int, int]]:
    output: list[tuple[int, int, int, int]] = []
    for region in regions:
        x = min(max(0, region.x), max(0, width - 3))
        y = min(max(0, region.y), max(0, height - 3))
        box_width = min(region.width, width - x)
        box_height = min(region.height, height - y)
        if box_width >= 3 and box_height >= 3:
            output.append((x, y, box_width, box_height))
    return output


def _tracking_templates(frame: object, regions: Sequence[tuple[int, int, int, int]]):
    import cv2
    import numpy as np

    templates: list[object] = []
    uses_edges: list[bool] = []
    for x, y, width, height in regions:
        crop = frame[y : y + height, x : x + width]  # type: ignore[index]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        use_edges = int(np.count_nonzero(edges)) >= 12
        templates.append(edges if use_edges else gray)
        uses_edges.append(use_edges)
    return templates, uses_edges


def _dynamic_fine_masks(
    frame: object, regions: Sequence[tuple[int, int, int, int]]
) -> list[object]:
    import cv2
    import numpy as np

    masks: list[object] = []
    for x, y, width, height in regions:
        crop = frame[y : y + height, x : x + width]  # type: ignore[index]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 45, 140)
        kernel_size = max(5, int(round(min(width, height) * 0.14)))
        if kernel_size % 2 == 0:
            kernel_size += 1
        mask = cv2.dilate(
            edges, np.ones((kernel_size, kernel_size), np.uint8), iterations=1
        )
        # Filled platform pills and circular avatars must be removed as a
        # whole. Inpainting only their outlines leaves the badge fill or
        # profile photo visible. Text-only boxes have no dominant enclosed
        # contour and keep the more conservative stroke mask.
        contours, _ = cv2.findContours(
            mask.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        for contour in contours:
            if cv2.contourArea(contour) >= width * height * 0.18:
                cv2.drawContours(mask, [contour], -1, 255, thickness=-1)
        masks.append(_safe_local_mask(mask, width, height))
    return masks


def _static_fine_masks(
    source: Path, regions: Sequence[tuple[int, int, int, int]]
) -> list[object]:
    import cv2
    import numpy as np

    samples = _sample_video(source, 16)
    layers: list[list[object]] = [[] for _ in regions]
    for frame in samples.frames:
        for index, (x, y, width, height) in enumerate(regions):
            crop = frame[y : y + height, x : x + width]
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            layers[index].append(cv2.Canny(gray, 45, 140) > 0)
    masks: list[object] = []
    for (x, y, width, height), edge_layers in zip(regions, layers, strict=True):
        persistence = np.mean(np.stack(edge_layers, axis=0), axis=0)
        mask = (persistence >= 0.50).astype(np.uint8) * 255
        kernel_size = max(5, int(round(min(width, height) * 0.14)))
        if kernel_size % 2 == 0:
            kernel_size += 1
        mask = cv2.dilate(
            mask, np.ones((kernel_size, kernel_size), np.uint8), iterations=1
        )
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1
        )
        masks.append(_safe_local_mask(mask, width, height))
    return masks


def _safe_local_mask(mask: object, width: int, height: int):
    import cv2
    import numpy as np

    coverage = float(np.count_nonzero(mask)) / max(1, width * height)
    if coverage < 0.01:
        fallback = np.zeros((height, width), dtype=np.uint8)
        cv2.rectangle(fallback, (1, 1), (width - 2, height - 2), 255, thickness=-1)
        return fallback
    if coverage > 0.72:
        # Avoid erasing the entire coarse detection box when scene edges were
        # accidentally included; retain only the strongest central strokes.
        return cv2.erode(mask, np.ones((3, 3), np.uint8), iterations=1)
    return mask


def _tracked_frame_mask(
    frame: object,
    box: tuple[int, int, int, int],
    anchor_mask: object,
    frame_width: int,
    frame_height: int,
):
    """Refresh a moving overlay mask when its tracked box touches an edge.

    A watermark that jumps partly outside the frame cannot preserve the pixel
    alignment of the full anchor template. The edge-near box is expanded to
    adjacent boundaries before strokes are re-extracted from the current
    frame; ordinary interior frames retain the stable anchor mask.
    """

    import cv2

    x, y, width, height = box
    left_gap = x
    top_gap = y
    right_gap = frame_width - (x + width)
    bottom_gap = frame_height - (y + height)
    near_edge = (
        left_gap <= max(2, width // 3)
        or right_gap <= max(2, width // 3)
        or top_gap <= max(2, height)
        or bottom_gap <= max(2, height)
    )
    if not near_edge:
        return box, anchor_mask

    pad_x = max(4, width // 4)
    pad_y = max(4, height // 2)
    left = max(0, x - pad_x)
    top = max(0, y - pad_y)
    right = min(frame_width, x + width + pad_x)
    bottom = min(frame_height, y + height + pad_y)
    if left_gap <= max(2, width // 3):
        left = 0
    if right_gap <= max(2, width // 3):
        right = frame_width
    if top_gap <= max(2, height):
        top = 0
    if bottom_gap <= max(2, height):
        bottom = frame_height
    expanded = (left, top, right - left, bottom - top)
    refreshed = _dynamic_fine_masks(frame, [expanded])[0]
    anchor_layer = refreshed.copy()
    anchor_left = x - left
    anchor_top = y - top
    anchor_layer[
        anchor_top : anchor_top + height,
        anchor_left : anchor_left + width,
    ] = cv2.max(
        anchor_layer[
            anchor_top : anchor_top + height,
            anchor_left : anchor_left + width,
        ],
        anchor_mask,
    )
    return expanded, anchor_layer


def _track_template(
    gray_frame: object,
    template: object,
    use_edges: bool,
    x: int,
    y: int,
    width: int,
    height: int,
    search_radius: int,
) -> tuple[int, int]:
    import cv2

    frame_height, frame_width = gray_frame.shape  # type: ignore[attr-defined]
    left = max(0, x - search_radius)
    top = max(0, y - search_radius)
    right = min(frame_width, x + width + search_radius)
    bottom = min(frame_height, y + height + search_radius)
    search_gray = gray_frame[top:bottom, left:right]  # type: ignore[index]
    if template.shape[0] > search_gray.shape[0] or template.shape[1] > search_gray.shape[1]:
        return x, y
    search = cv2.Canny(search_gray, 50, 150) if use_edges else search_gray
    scores = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, location = cv2.minMaxLoc(scores)
    if score < 0.55:
        full_search = cv2.Canny(gray_frame, 50, 150) if use_edges else gray_frame
        full_scores = cv2.matchTemplate(full_search, template, cv2.TM_CCOEFF_NORMED)
        _, global_score, _, global_location = cv2.minMaxLoc(full_scores)
        if global_score >= 0.55 or global_score >= max(0.22, score + 0.08):
            return int(global_location[0]), int(global_location[1])
        edge_match = _match_partial_template_at_frame_edge(
            full_search,
            template,
            width,
            height,
        )
        if edge_match is not None:
            edge_x, edge_y, edge_score = edge_match
            if edge_score >= 0.58 or edge_score >= max(0.38, global_score + 0.12):
                return edge_x, edge_y
    if score < 0.18:
        return x, y
    return left + int(location[0]), top + int(location[1])


def _match_partial_template_at_frame_edge(
    search: object,
    template: object,
    box_width: int,
    box_height: int,
) -> tuple[int, int, float] | None:
    """Relocate an overlay when only its entering edge fragment is visible.

    Moving watermarks often disappear and then re-enter from a corner. A full
    anchor template cannot match those first clipped frames, so compare its
    visible halves and corner fragments while constraining matches to the
    corresponding frame edge.
    """

    import cv2

    frame_height, frame_width = search.shape  # type: ignore[attr-defined]
    template_height, template_width = template.shape  # type: ignore[attr-defined]
    horizontal_cut = max(8, int(round(template_width * 0.50)))
    vertical_cut = max(8, int(round(template_height * 0.55)))
    horizontal_cut = min(template_width, horizontal_cut)
    vertical_cut = min(template_height, vertical_cut)
    horizontal_modes = [
        ("right", 0, horizontal_cut),
        ("left", template_width - horizontal_cut, template_width),
    ]
    vertical_modes = [
        ("bottom", 0, vertical_cut),
        ("top", template_height - vertical_cut, template_height),
    ]
    candidates: list[tuple[str | None, str | None, int, int, int, int]] = []
    for edge, start, end in horizontal_modes:
        candidates.append((edge, None, start, end, 0, template_height))
    for edge, start, end in vertical_modes:
        candidates.append((None, edge, 0, template_width, start, end))
    for horizontal_edge, left, right in horizontal_modes:
        for vertical_edge, top, bottom in vertical_modes:
            candidates.append(
                (horizontal_edge, vertical_edge, left, right, top, bottom)
            )

    best: tuple[int, int, float] | None = None
    horizontal_margin = max(6, box_width // 3)
    vertical_margin = max(6, box_height)
    for horizontal_edge, vertical_edge, left, right, top, bottom in candidates:
        partial = template[top:bottom, left:right]  # type: ignore[index]
        partial_height, partial_width = partial.shape
        if partial_width > frame_width or partial_height > frame_height:
            continue
        search_left = 0
        search_right = frame_width
        search_top = 0
        search_bottom = frame_height
        if horizontal_edge == "right":
            search_left = max(0, frame_width - partial_width - horizontal_margin)
        elif horizontal_edge == "left":
            search_right = min(frame_width, partial_width + horizontal_margin)
        if vertical_edge == "bottom":
            search_top = max(0, frame_height - partial_height - vertical_margin)
        elif vertical_edge == "top":
            search_bottom = min(frame_height, partial_height + vertical_margin)
        edge_search = search[
            search_top:search_bottom,
            search_left:search_right,
        ]  # type: ignore[index]
        if (
            edge_search.shape[1] < partial_width
            or edge_search.shape[0] < partial_height
        ):
            continue
        scores = cv2.matchTemplate(edge_search, partial, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(scores)
        location_x = search_left + int(location[0])
        location_y = search_top + int(location[1])
        if horizontal_edge == "right" and (
            location_x + partial_width < frame_width - horizontal_margin
        ):
            continue
        if horizontal_edge == "left" and location_x > horizontal_margin:
            continue
        if vertical_edge == "bottom" and (
            location_y + partial_height < frame_height - vertical_margin
        ):
            continue
        if vertical_edge == "top" and location_y > vertical_margin:
            continue
        origin_x = max(0, min(frame_width - box_width, location_x - left))
        origin_y = max(0, min(frame_height - box_height, location_y - top))
        if best is None or score > best[2]:
            best = (int(origin_x), int(origin_y), float(score))
    return best


def _build_bidirectional_track(
    source: Path,
    template: object,
    use_edges: bool,
    anchor: tuple[int, int, int, int],
    anchor_index: int,
    frame_count: int,
    search_radius: int,
) -> list[tuple[int, int, int, int]]:
    import cv2

    positions = [anchor] * frame_count
    x, y, width, height = anchor
    capture = cv2.VideoCapture(str(source))
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, anchor_index)
        for index in range(anchor_index, frame_count):
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            if index != anchor_index:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                x, y = _track_template(
                    gray, template, use_edges, x, y, width, height, search_radius
                )
            positions[index] = (x, y, width, height)

        x, y, width, height = anchor
        for index in range(anchor_index - 1, -1, -1):
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            x, y = _track_template(
                gray, template, use_edges, x, y, width, height, search_radius
            )
            positions[index] = (x, y, width, height)
    finally:
        capture.release()
    return positions


def _blend_temporal_reference(
    previous_gray: object,
    current_gray: object,
    previous_repaired: object,
    repaired: object,
    mask: object,
):
    import cv2
    import numpy as np

    height, width = current_gray.shape  # type: ignore[attr-defined]
    flow_width = min(480, width)
    scale = flow_width / width
    flow_height = max(1, int(height * scale))
    previous_small = cv2.resize(previous_gray, (flow_width, flow_height))
    current_small = cv2.resize(current_gray, (flow_width, flow_height))
    # Current -> previous flow lets remap sample the previous repaired frame in
    # the coordinate system of the current frame.
    flow = cv2.calcOpticalFlowFarneback(
        current_small,
        previous_small,
        None,
        0.5,
        3,
        15,
        3,
        5,
        1.2,
        0,
    )
    grid_x, grid_y = np.meshgrid(
        np.arange(flow_width, dtype=np.float32),
        np.arange(flow_height, dtype=np.float32),
    )
    previous_small_color = cv2.resize(previous_repaired, (flow_width, flow_height))
    warped_small = cv2.remap(
        previous_small_color,
        grid_x + flow[..., 0],
        grid_y + flow[..., 1],
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )
    warped = cv2.resize(warped_small, (width, height), interpolation=cv2.INTER_LINEAR)
    alpha = cv2.GaussianBlur(mask, (0, 0), sigmaX=2.0).astype(np.float32) / 255.0
    alpha = np.clip(alpha * 0.28, 0.0, 0.28)[..., None]
    return np.clip(
        repaired.astype(np.float32) * (1.0 - alpha)
        + warped.astype(np.float32) * alpha,
        0,
        255,
    ).astype(np.uint8)


def _mux_repaired_video(
    ffmpeg: str,
    temporary: Path,
    source: Path,
    destination: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(temporary),
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    completed = runner(command, capture_output=True, text=True, timeout=3_600, check=False)
    temporary.unlink(missing_ok=True)
    if completed.returncode != 0 or not destination.is_file():
        destination.unlink(missing_ok=True)
        detail = (completed.stderr or "FFmpeg did not create an output file").strip()[-1_000:]
        raise AnalyzerError(ErrorCode.WATERMARK_REMOVAL_FAILED, detail)


def _remove_tracked_regions(
    ffmpeg: str,
    source: Path,
    destination: Path,
    regions: Sequence[WatermarkRegion],
    search_radius: int,
    inpaint_radius: int,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Track manually confirmed overlays and inpaint their changing positions."""

    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise AnalyzerError(
            ErrorCode.CONFIGURATION_ERROR,
            "OpenCV is required for moving-watermark tracking",
        ) from exc

    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise AnalyzerError(ErrorCode.UNSUPPORTED_MEDIA, f"cannot open video: {source.name}")
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0) or 25.0
    ok, first_frame = capture.read()
    if not ok or first_frame is None:
        capture.release()
        raise AnalyzerError(ErrorCode.UNSUPPORTED_MEDIA, "cannot read first video frame")

    normalized: list[tuple[int, int, int, int]] = []
    templates: list[object] = []
    template_uses_edges: list[bool] = []
    for region in regions:
        x = min(max(0, region.x), max(0, width - 3))
        y = min(max(0, region.y), max(0, height - 3))
        box_width = min(region.width, width - x)
        box_height = min(region.height, height - y)
        if box_width < 3 or box_height < 3:
            continue
        normalized.append((x, y, box_width, box_height))
        crop = first_frame[y : y + box_height, x : x + box_width]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        use_edges = int(np.count_nonzero(edges)) >= 12
        templates.append(edges if use_edges else gray)
        template_uses_edges.append(use_edges)
    if not normalized:
        capture.release()
        raise AnalyzerError(ErrorCode.WATERMARK_REMOVAL_FAILED, "no valid tracked regions")

    temporary = destination.with_name(f".{destination.stem}.tracking-video.mp4")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(temporary),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        capture.release()
        raise AnalyzerError(ErrorCode.WATERMARK_REMOVAL_FAILED, "cannot create tracking video")

    positions = list(normalized)
    try:
        frame = first_frame
        while frame is not None:
            gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            mask = np.zeros((height, width), dtype=np.uint8)
            next_positions: list[tuple[int, int, int, int]] = []
            for template, use_edges, (x, y, box_width, box_height) in zip(
                templates, template_uses_edges, positions, strict=True
            ):
                left = max(0, x - search_radius)
                top = max(0, y - search_radius)
                right = min(width, x + box_width + search_radius)
                bottom = min(height, y + box_height + search_radius)
                search_gray = gray_frame[top:bottom, left:right]
                if template.shape[0] <= search_gray.shape[0] and template.shape[1] <= search_gray.shape[1]:
                    search = cv2.Canny(search_gray, 50, 150) if use_edges else search_gray
                    scores = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
                    _, score, _, location = cv2.minMaxLoc(scores)
                    if score >= 0.18:
                        x = left + int(location[0])
                        y = top + int(location[1])
                next_positions.append((x, y, box_width, box_height))
                padding = max(2, min(box_width, box_height) // 20)
                cv2.rectangle(
                    mask,
                    (max(0, x - padding), max(0, y - padding)),
                    (min(width - 1, x + box_width + padding), min(height - 1, y + box_height + padding)),
                    255,
                    thickness=-1,
                )
            positions = next_positions
            repaired = cv2.inpaint(frame, mask, float(inpaint_radius), cv2.INPAINT_TELEA)
            writer.write(repaired)
            ok, frame = capture.read()
            if not ok:
                frame = None
    finally:
        writer.release()
        capture.release()

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(temporary),
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    completed = runner(command, capture_output=True, text=True, timeout=3_600, check=False)
    temporary.unlink(missing_ok=True)
    if completed.returncode != 0 or not destination.is_file():
        destination.unlink(missing_ok=True)
        detail = (completed.stderr or "FFmpeg did not create an output file").strip()[-1_000:]
        raise AnalyzerError(ErrorCode.WATERMARK_REMOVAL_FAILED, detail)


def _find_ffmpeg() -> str | None:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _is_video(manifest: ArtifactRef, path: Path) -> bool:
    if (manifest.media_type or "").startswith("video/"):
        return True
    return path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
