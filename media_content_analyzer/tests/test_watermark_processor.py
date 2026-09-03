from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from media_content_analyzer import (
    ArtifactRef,
    InMemoryAuditSink,
    MediaWatermarkProcessorTool,
    ProcessWatermarkInput,
    ProcessWatermarkOutput,
    WatermarkRegion,
)
from media_content_analyzer.errors import AnalyzerError, ErrorCode
from media_content_analyzer.ports import ToolContext
from media_content_analyzer.watermark_processor import (
    OpenCvWatermarkBackend,
    VideoSamples,
    _detect_dynamic_regions,
    _detect_static_regions,
    _dynamic_fine_masks,
    _automatic_sample_frame_count,
    _static_sample_support_bonus,
    _track_template,
    _tracked_frame_mask,
)


def _manifest(path: Path) -> ArtifactRef:
    return ArtifactRef(
        path=str(path),
        size_bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        media_type="video/mp4",
    )


def _context() -> ToolContext:
    return ToolContext(
        tenant_id="local",
        trace_id="watermark-test",
        actor_type="user",
        actor_id="tester",
    )


def test_automatic_sample_count_scales_with_duration() -> None:
    assert _automatic_sample_frame_count(3.0, 90) == 36
    assert _automatic_sample_frame_count(28.3, 849) == 57
    assert _automatic_sample_frame_count(300.0, 9_000) == 120
    assert _automatic_sample_frame_count(2.0, 24) == 24


def test_dense_static_sampling_adds_bounded_confidence_support() -> None:
    assert _static_sample_support_bonus(18) == 0.0
    assert _static_sample_support_bonus(36) == pytest.approx(0.00923, abs=0.00001)
    assert _static_sample_support_bonus(57) == 0.02
    assert _static_sample_support_bonus(120) == 0.02


def test_dense_sampling_keeps_translucent_static_overlay_above_standard_threshold() -> None:
    frames = []
    height, width = 360, 640
    yy, xx = np.mgrid[:height, :width]
    for index in range(57):
        base = ((xx * 0.31 + yy * 0.17 + index * 9) % 180 + 30).astype(np.uint8)
        frame = np.dstack(
            [base, np.roll(base, index * 3, axis=1), np.roll(base, index * 2, axis=0)]
        )
        overlay = frame.copy()
        cv2.putText(
            overlay,
            "tg:@account",
            (40, 330),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.85,
            (245, 245, 245),
            2,
            cv2.LINE_AA,
        )
        frames.append(cv2.addWeighted(overlay, 0.306, frame, 0.694, 0))

    regions = _detect_static_regions(VideoSamples(frames, width, height, 28.5))

    assert regions
    assert regions[0].confidence >= 0.72


def test_static_corner_overlay_is_detected_from_changing_frames() -> None:
    random = np.random.default_rng(4)
    frames = []
    for _ in range(18):
        frame = random.integers(0, 100, (360, 640, 3), dtype=np.uint8)
        cv2.putText(
            frame,
            "BRAND",
            (500, 330),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        frames.append(frame)

    regions = _detect_static_regions(VideoSamples(frames, 640, 360, 10.0))

    assert regions
    assert regions[0].x >= 450
    assert regions[0].y >= 280
    assert regions[0].confidence >= 0.8


def test_static_center_overlay_is_detected_from_changing_frames() -> None:
    random = np.random.default_rng(8)
    frames = []
    for _ in range(18):
        frame = random.integers(0, 150, (360, 640, 3), dtype=np.uint8)
        cv2.putText(
            frame,
            "TG@ACCOUNT",
            (225, 190),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        frames.append(frame)

    regions = _detect_static_regions(VideoSamples(frames, 640, 360, 10.0))

    assert regions
    assert 180 <= regions[0].x <= 260
    assert 140 <= regions[0].y <= 210
    assert regions[0].confidence >= 0.8


def test_static_detection_keeps_weak_disconnected_leading_glyph_in_context() -> None:
    random = np.random.default_rng(21)
    frames = []
    for _ in range(57):
        frame = random.integers(15, 125, (360, 640, 3), dtype=np.uint8)
        strong = frame.copy()
        cv2.putText(
            strong,
            "g:@account",
            (70, 330),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.85,
            (250, 250, 250),
            2,
            cv2.LINE_AA,
        )
        frame = cv2.addWeighted(strong, 0.72, frame, 0.28, 0)
        weak = frame.copy()
        cv2.putText(
            weak,
            "t",
            (40, 330),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.85,
            (245, 245, 245),
            2,
            cv2.LINE_AA,
        )
        frames.append(cv2.addWeighted(weak, 0.28, frame, 0.72, 0))

    regions = _detect_static_regions(VideoSamples(frames, 640, 360, 28.5))

    assert regions
    assert regions[0].x <= 40
    assert regions[0].x + regions[0].width >= 200


def test_tracked_mask_refreshes_visible_strokes_at_frame_edge() -> None:
    frame = np.zeros((80, 120, 3), dtype=np.uint8)
    cv2.putText(
        frame,
        "@WM",
        (82, 43),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    box = (60, 15, 60, 40)
    anchor_mask = np.zeros((40, 60), dtype=np.uint8)
    anchor_mask[5:8, 5:8] = 255

    refreshed_box, refreshed = _tracked_frame_mask(frame, box, anchor_mask, 120, 80)

    assert refreshed_box[2] > box[2]
    assert refreshed_box[0] < box[0]
    assert np.count_nonzero(refreshed) > np.count_nonzero(anchor_mask)
    assert np.count_nonzero(refreshed[:, 20:]) > 0


def test_tracked_mask_keeps_anchor_for_interior_box() -> None:
    frame = np.zeros((100, 160, 3), dtype=np.uint8)
    anchor_mask = np.zeros((20, 40), dtype=np.uint8)
    anchor_mask[4:10, 5:12] = 255

    box = (50, 40, 40, 20)
    result_box, result = _tracked_frame_mask(frame, box, anchor_mask, 160, 100)

    assert result_box == box
    assert result is anchor_mask


def test_tracker_relocalizes_first_clipped_corner_frame() -> None:
    template_canvas = np.zeros((42, 120), dtype=np.uint8)
    cv2.putText(
        template_canvas,
        "@EDGE",
        (3, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.85,
        255,
        2,
        cv2.LINE_AA,
    )
    template = cv2.Canny(template_canvas, 50, 150)
    frame = np.zeros((120, 180), dtype=np.uint8)
    # Only the upper-left fragment is visible while the overlay enters from
    # the bottom-right corner.
    frame[96:120, 112:180] = template_canvas[:24, :68]

    x, y = _track_template(
        frame,
        template,
        True,
        10,
        10,
        120,
        42,
        8,
    )

    assert x >= 55
    assert y >= 75


def test_moving_text_overlay_is_detected_automatically() -> None:
    frames = []
    height, width = 360, 640
    yy, xx = np.mgrid[:height, :width]
    for index in range(18):
        base = ((xx * 0.15 + yy * 0.2 + index * 8) % 180 + 30).astype(np.uint8)
        frame = np.dstack(
            [base, np.roll(base, index * 3, axis=1), np.roll(base, index * 2, axis=0)]
        )
        cv2.circle(frame, (320 + index * 3, 180), 90, (100, 40 + index * 5, 160), -1)
        cv2.putText(
            frame,
            "TG@YYTsir",
            (20 + index * 20, 70 + (index % 4) * 7),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        frames.append(frame)

    regions = _detect_dynamic_regions(VideoSamples(frames, width, height, 10.0))

    assert regions
    assert regions[0].x < 40
    assert regions[0].y < 70
    assert regions[0].confidence >= 0.75


def test_compound_corner_watermark_detects_badge_text_and_avatar() -> None:
    frames = []
    height, width = 360, 640
    random = np.random.default_rng(29)
    avatar = np.full((50, 50, 3), 30, dtype=np.uint8)
    cv2.circle(avatar, (25, 25), 23, (210, 80, 35), -1)
    cv2.circle(avatar, (25, 25), 23, (255, 255, 255), 2)
    cv2.circle(avatar, (25, 19), 8, (90, 210, 245), -1)
    cv2.ellipse(avatar, (25, 38), (15, 10), 0, 180, 360, (90, 210, 245), -1)
    for index in range(24):
        frame = random.integers(55, 85, (height, width, 3), dtype=np.uint8)
        if index < 12:
            badge_x, badge_y = 520, 304
            text_x, text_y = 60, 340
            avatar_x, avatar_y = 20, 285
        else:
            badge_x, badge_y = 16, 14
            text_x, text_y = 455, 49
            avatar_x, avatar_y = 570, 20
        cv2.rectangle(
            frame,
            (badge_x, badge_y),
            (badge_x + 96, badge_y + 40),
            (250, 250, 250),
            -1,
        )
        cv2.putText(
            frame,
            "APP",
            (badge_x + 13, badge_y + 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (10, 10, 10),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            "ACCOUNT",
            (text_x, text_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (250, 250, 250),
            2,
            cv2.LINE_AA,
        )
        frame[avatar_y : avatar_y + 50, avatar_x : avatar_x + 50] = avatar
        frames.append(frame)

    regions = _detect_dynamic_regions(VideoSamples(frames, width, height, 8.0))

    assert len(regions) >= 3
    assert any(region.width >= 90 and region.height >= 35 for region in regions)
    assert any(abs(region.width - region.height) <= 15 for region in regions)


def test_dynamic_mask_fills_solid_badge_interior() -> None:
    frame = np.full((80, 160, 3), 70, dtype=np.uint8)
    cv2.rectangle(frame, (10, 15), (150, 65), (250, 250, 250), -1)
    cv2.putText(
        frame,
        "APP",
        (45, 51),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (10, 10, 10),
        2,
        cv2.LINE_AA,
    )

    mask = _dynamic_fine_masks(frame, [(5, 10, 150, 60)])[0]

    assert np.count_nonzero(mask) / mask.size >= 0.55


def test_cyclic_scrolling_overlay_can_start_after_first_sample() -> None:
    frames = []
    height, width = 360, 640
    yy, xx = np.mgrid[:height, :width]
    positions = [(40, 55), (180, 140), (340, 235), (40, 55)]
    for index in range(18):
        base = ((xx * 0.11 + yy * 0.19 + index * 7) % 170 + 30).astype(np.uint8)
        frame = np.dstack([base, np.roll(base, index, axis=1), base])
        if index > 0:
            x, y = positions[(index - 1) % len(positions)]
            cv2.putText(
                frame,
                "@SCROLLING",
                (x, y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.72,
                (240, 240, 240),
                2,
                cv2.LINE_AA,
            )
        frames.append(frame)

    regions = _detect_dynamic_regions(VideoSamples(frames, width, height, 18.0))

    assert regions
    assert regions[0].first_seen_seconds is not None
    assert regions[0].first_seen_seconds > 0
    assert regions[0].confidence >= 0.84


def test_removal_requires_explicit_authorization(tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")

    class Backend:
        detector_version = "fake"

        def process(self, request, artifacts, output_directory):
            raise AssertionError("backend must not run")

    tool = MediaWatermarkProcessorTool(
        backend=Backend(),
        audit_sink=InMemoryAuditSink(),
        allowed_media_root=tmp_path,
        output_root=tmp_path / "output",
    )
    request = ProcessWatermarkInput(
        artifacts=[_manifest(source)],
        mode="remove_if_present",
        authorization_confirmed=False,
    )

    with pytest.raises(AnalyzerError) as raised:
        asyncio.run(tool.execute(request, _context()))
    assert raised.value.code is ErrorCode.AUTHORIZATION_REQUIRED
    assert source.read_bytes() == b"video"


def test_manual_region_creates_derivative_and_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "original.mp4"
    source.write_bytes(b"original-video")
    manifest = _manifest(source)
    commands = []

    monkeypatch.setattr(
        "media_content_analyzer.watermark_processor._sample_video",
        lambda path, count: VideoSamples([object()] * 8, 640, 360, 10.0),
    )

    def runner(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"processed-video")
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = OpenCvWatermarkBackend(ffmpeg_path="/fake/ffmpeg", runner=runner)
    request = ProcessWatermarkInput(
        artifacts=[manifest],
        mode="remove_if_present",
        repair_quality="fast",
        authorization_confirmed=True,
        manual_regions={
            manifest.sha256: [
                WatermarkRegion(x=500, y=300, width=100, height=30, confidence=0.99)
            ]
        },
    )

    output = backend.process(request, [source], tmp_path / "derived")

    assert output.processed_count == 1
    assert output.items[0].processed_artifact is not None
    assert source.read_bytes() == b"original-video"
    assert "delogo=x=500:y=300:w=100:h=30:show=0" in commands[0]


def test_manual_moving_region_uses_tracking_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "moving.mp4"
    source.write_bytes(b"original-video")
    manifest = _manifest(source)
    tracked: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "media_content_analyzer.watermark_processor._sample_video",
        lambda path, count: VideoSamples([object()] * 8, 640, 360, 10.0),
    )

    def fake_tracking(ffmpeg, source, destination, regions, radius, inpaint, runner):
        tracked.append((radius, inpaint))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"tracked-video")

    monkeypatch.setattr(
        "media_content_analyzer.watermark_processor._remove_tracked_regions",
        fake_tracking,
    )
    backend = OpenCvWatermarkBackend(ffmpeg_path="/fake/ffmpeg")
    request = ProcessWatermarkInput(
        artifacts=[manifest],
        mode="remove_if_present",
        repair_quality="fast",
        authorization_confirmed=True,
        track_manual_regions=True,
        tracking_search_radius=180,
        inpaint_radius=7,
        manual_regions={
            str(source): [WatermarkRegion(x=100, y=80, width=120, height=36)]
        },
    )

    output = backend.process(request, [source], tmp_path / "derived")

    assert tracked == [(180, 7)]
    assert output.items[0].kind == "moving"
    assert output.items[0].needs_human_review
    assert output.processed_count == 1


def test_high_quality_repair_uses_configured_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "high.mp4"
    source.write_bytes(b"original-video")
    manifest = _manifest(source)
    calls: list[tuple[bool, int]] = []

    monkeypatch.setattr(
        "media_content_analyzer.watermark_processor._sample_video",
        lambda path, count: VideoSamples([object()] * 8, 640, 360, 10.0),
    )

    class HighQualityBackend:
        name = "test-video-inpainting-worker"

        def repair(self, *, source, destination, regions, moving, tracked_regions=None):
            calls.append((moving, len(regions)))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"high-quality-video")

    backend = OpenCvWatermarkBackend(
        ffmpeg_path="/fake/ffmpeg",
        high_quality_backend=HighQualityBackend(),
    )
    output = backend.process(
        ProcessWatermarkInput(
            artifacts=[manifest],
            mode="remove_if_present",
            repair_quality="high",
            authorization_confirmed=True,
            manual_regions={
                str(source): [WatermarkRegion(x=100, y=80, width=120, height=36)]
            },
        ),
        [source],
        tmp_path / "derived-high",
    )

    assert calls == [(False, 1)]
    assert output.items[0].repair_quality_applied == "high"
    assert output.items[0].repair_method == "test-video-inpainting-worker"


def test_high_quality_without_worker_falls_back_to_balanced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "fallback.mp4"
    source.write_bytes(b"original-video")
    manifest = _manifest(source)
    monkeypatch.setattr(
        "media_content_analyzer.watermark_processor._sample_video",
        lambda path, count: VideoSamples([object()] * 8, 640, 360, 10.0),
    )

    def fake_temporal(ffmpeg, source, destination, regions, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"balanced-video")

    monkeypatch.setattr(
        "media_content_analyzer.watermark_processor._remove_temporal_regions",
        fake_temporal,
    )
    backend = OpenCvWatermarkBackend(ffmpeg_path="/fake/ffmpeg")
    output = backend.process(
        ProcessWatermarkInput(
            artifacts=[manifest],
            mode="remove_if_present",
            repair_quality="high",
            authorization_confirmed=True,
            manual_regions={
                str(source): [WatermarkRegion(x=100, y=80, width=120, height=36)]
            },
        ),
        [source],
        tmp_path / "derived-fallback",
    )

    assert output.items[0].repair_quality_applied == "balanced"
    assert output.items[0].repair_method == "opencv-fine-mask-temporal-v1"
    assert any("Worker" in warning for warning in output.items[0].warnings)


def test_automatic_moving_overlay_creates_tracked_derivative(tmp_path: Path) -> None:
    source = tmp_path / "moving-auto.mp4"
    width, height = 480, 270
    writer = cv2.VideoWriter(
        str(source), cv2.VideoWriter_fourcc(*"mp4v"), 24, (width, height)
    )
    yy, xx = np.mgrid[:height, :width]
    for index in range(72):
        base = ((xx * 0.16 + yy * 0.23 + index * 4) % 160 + 35).astype(np.uint8)
        frame = np.dstack([base, np.roll(base, index, axis=1), np.roll(base, index, axis=0)])
        cv2.putText(
            frame,
            "TG@AUTO",
            (18 + index * 4, 62 + (index % 3) * 3),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        writer.write(frame)
    writer.release()
    assert source.is_file()
    manifest = _manifest(source)
    backend = OpenCvWatermarkBackend()

    output = backend.process(
        ProcessWatermarkInput(
            artifacts=[manifest],
            mode="remove_if_present",
            authorization_confirmed=True,
            sample_frames=18,
        ),
        [source],
        tmp_path / "derived-auto",
    )

    assert output.items[0].kind == "moving"
    assert output.items[0].processed_artifact is not None
    assert Path(output.items[0].processed_artifact.path).is_file()
    assert output.items[0].repair_quality_applied == "balanced"
    assert output.items[0].repair_method == "opencv-fine-mask-temporal-v1"
    assert output.items[0].needs_human_review


def test_detect_only_tool_removes_empty_work_directory(tmp_path: Path) -> None:
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")

    class Backend:
        detector_version = "fake-v1"

        def process(self, request, artifacts, output_directory):
            return ProcessWatermarkOutput(
                items=[],
                detected_count=0,
                processed_count=0,
                detector_version=self.detector_version,
            )

    output_root = tmp_path / "output"
    audit = InMemoryAuditSink()
    tool = MediaWatermarkProcessorTool(
        backend=Backend(),
        audit_sink=audit,
        allowed_media_root=tmp_path,
        output_root=output_root,
    )

    result = asyncio.run(
        tool.execute(ProcessWatermarkInput(artifacts=[_manifest(source)]), _context())
    )

    assert result.processed_count == 0
    assert list(output_root.rglob("*")) == [output_root / "local"] or not list(
        output_root.rglob("*")
    )
    assert audit.events[-1].tool_name == "media.process_watermark"
