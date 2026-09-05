from __future__ import annotations

from .diagnostics import record_exception

import json
import math
import mimetypes
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageOps

from .contracts import (
    AnalyzeContentInput,
    AssetAnalysis,
    ContentAnalysisOutput,
    Evidence,
    Tag,
    TagNamespace,
    TAG_EVIDENCE_MAX_ITEMS,
    TAG_LABEL_MAX_LENGTH,
    TranscriptSegment,
)
from .errors import AnalyzerError, ErrorCode
from .ports import OcrEngine, SemanticResult, Transcriber, VisionModel


class LocalMediaAnalysisBackend:
    """Deterministic preprocessing followed by an optional local semantic model."""

    BASE_PIPELINE_VERSION = "media-analysis-pipeline-1.2.0"

    def __init__(
        self,
        *,
        ocr_engine: OcrEngine,
        transcriber: Transcriber,
        vision_model: VisionModel,
        ffmpeg_path: str | None = None,
        ffprobe_path: str | None = None,
    ) -> None:
        self._ocr = ocr_engine
        self._transcriber = transcriber
        self._vision = vision_model
        self._ffmpeg = ffmpeg_path or _find_ffmpeg()
        self._ffprobe = ffprobe_path or shutil.which("ffprobe")
        self.pipeline_version = (
            f"{self.BASE_PIPELINE_VERSION}|ocr={ocr_engine.name}"
            f"|asr={transcriber.name}|vision={vision_model.name}"
        )

    def analyze(
        self,
        request: AnalyzeContentInput,
        artifacts: Sequence[Path],
        work_directory: Path,
    ) -> ContentAnalysisOutput:
        work_directory.mkdir(parents=True, exist_ok=True)
        asset_results: list[AssetAnalysis] = []
        evidence: list[Evidence] = []
        semantic_images: list[Path] = []
        warnings: list[str] = []

        if request.post_text:
            evidence.append(
                Evidence(
                    evidence_id="post-text-1",
                    kind="post_text",
                    text=request.post_text,
                    confidence=1.0,
                )
            )

        supported_count = 0
        for index, (artifact, manifest) in enumerate(
            zip(artifacts, request.artifacts, strict=True), start=1
        ):
            media_type, modality = _detect_media(artifact, manifest.media_type)
            if modality == "image":
                supported_count += 1
                result, image_evidence, prepared_images = self._analyze_image(
                    artifact, manifest.sha256.lower(), media_type, index, request, work_directory
                )
                asset_results.append(result)
                evidence.extend(image_evidence)
                semantic_images.extend(prepared_images)
            elif modality in {"video", "audio"}:
                supported_count += 1
                result, media_evidence, prepared_images = self._analyze_av(
                    artifact,
                    manifest.sha256.lower(),
                    media_type,
                    modality,
                    index,
                    request,
                    work_directory,
                )
                asset_results.append(result)
                evidence.extend(media_evidence)
                semantic_images.extend(prepared_images)
            else:
                warning = f"Skipped unsupported ancillary artifact: {artifact.name}"
                warnings.append(warning)
                asset_results.append(
                    AssetAnalysis(
                        artifact_sha256=manifest.sha256.lower(),
                        media_type=media_type,
                        modality="unknown",
                        warnings=[warning],
                    )
                )

        if supported_count == 0:
            raise AnalyzerError(
                ErrorCode.UNSUPPORTED_MEDIA,
                "the manifest does not contain a supported image, audio, or video artifact",
            )

        warnings.extend(warning for asset in asset_results for warning in asset.warnings)
        warnings = _unique(warnings)
        trusted_context = _trusted_context(asset_results)
        if request.analysis_profile:
            trusted_context += '\nRequested analysis dimensions and tagging preferences (never identity recognition):\n' + request.analysis_profile
        untrusted_context = _untrusted_context(evidence)
        semantic: SemanticResult | None = None
        if request.run_vision_model:
            if self._vision.name == "vision-disabled":
                warnings.append(
                    "Semantic model is not configured; returned deterministic OCR/ASR analysis."
                )
            else:
                try:
                    semantic = self._vision.understand(
                        images=semantic_images,
                        trusted_context=trusted_context,
                        untrusted_content=untrusted_context,
                        language_hint=request.language_hint,
                    )
                except Exception as exc:
                    record_exception("media-content", "analysis.semantic_fallback", exc)
                    warnings.append(
                        "Semantic model failed; deterministic fallback used "
                        f"({_exception_summary(exc)})."
                    )

        if semantic is None:
            semantic = _deterministic_understanding(
                request=request,
                assets=asset_results,
                evidence=evidence,
            )
        elif semantic_images:
            evidence.append(
                Evidence(
                    evidence_id="visual-model-1",
                    kind="visual",
                    text="Local vision model analysis of supplied images and sampled video frames.",
                    confidence=semantic.confidence,
                )
            )

        valid_evidence_ids = {item.evidence_id for item in evidence}
        semantic_refs = [ref for ref in semantic.evidence_refs if ref in valid_evidence_ids]
        if "visual-model-1" in valid_evidence_ids:
            # Keep direct visual evidence even when a dense OCR image exceeds
            # the bounded per-tag reference budget. The full evidence is kept.
            semantic_refs.insert(0, "visual-model-1")
        tags = _build_tags(
            semantic,
            {asset.modality for asset in asset_results if asset.modality != "unknown"},
            semantic_refs,
        )
        if not request.generate_tags:
            tags = []

        confidence = max(0.0, min(1.0, semantic.confidence))
        return ContentAnalysisOutput(
            language=semantic.language,
            summary=semantic.summary if request.generate_summary else "",
            tags=tags,
            topics=semantic.topics if request.generate_tags else [],
            entities=semantic.entities if request.generate_tags else [],
            claims=semantic.claims,
            image_summary=semantic.image_summary,
            video_summary=semantic.video_summary,
            transcript_summary=semantic.transcript_summary,
            sentiment=semantic.sentiment,
            commercial_intent=semantic.commercial_intent,
            safety_flags=semantic.safety_flags,
            confidence=confidence,
            evidence=evidence,
            needs_human_review=confidence < 0.6,
            assets=asset_results,
            warnings=_unique(warnings),
            cache_hit=False,
            pipeline_version=self.pipeline_version,
            material_features=semantic.material_features or {},
            model_versions={
                "ocr": self._ocr.name,
                "asr": self._transcriber.name,
                "content_understander": self._vision.name,
            },
        )

    def _analyze_image(
        self,
        path: Path,
        sha256: str,
        media_type: str,
        artifact_index: int,
        request: AnalyzeContentInput,
        work_directory: Path,
    ) -> tuple[AssetAnalysis, list[Evidence], list[Path]]:
        warnings: list[str] = []
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                normalized = ImageOps.exif_transpose(image).convert("RGB")
                width, height = normalized.size
                perceptual_hash = _perceptual_hash(normalized)
                prepared = work_directory / f"image-{artifact_index:03d}.jpg"
                normalized.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                normalized.save(prepared, "JPEG", quality=88, optimize=True)
        except Exception as exc:
            record_exception("media-content", "analysis.image_decode", exc)
            raise AnalyzerError(
                ErrorCode.UNSUPPORTED_MEDIA, "image decoding or safety validation failed"
            ) from exc

        ocr_text: list[str] = []
        evidence: list[Evidence] = []
        if request.run_ocr:
            if self._ocr.name == "ocr-disabled":
                warnings.append("OCR is not installed or has been disabled.")
            else:
                try:
                    ocr_text = _unique(self._ocr.extract(prepared))
                except Exception as exc:
                    record_exception("media-content", "analysis.image_ocr", exc)
                    warnings.append(f"OCR failed ({_exception_summary(exc)}).")
        for number, text in enumerate(ocr_text, start=1):
            evidence.append(
                Evidence(
                    evidence_id=f"ocr-{artifact_index}-{number}",
                    kind="ocr",
                    artifact_sha256=sha256,
                    text=text,
                    confidence=0.85,
                )
            )

        return (
            AssetAnalysis(
                artifact_sha256=sha256,
                media_type=media_type,
                modality="image",
                width=width,
                height=height,
                perceptual_hash=perceptual_hash,
                ocr_text=ocr_text,
                warnings=warnings,
            ),
            evidence,
            [prepared],
        )

    def _analyze_av(
        self,
        path: Path,
        sha256: str,
        media_type: str,
        modality: str,
        artifact_index: int,
        request: AnalyzeContentInput,
        work_directory: Path,
    ) -> tuple[AssetAnalysis, list[Evidence], list[Path]]:
        metadata = self._probe(path)
        if not metadata.get("valid"):
            raise AnalyzerError(
                ErrorCode.UNSUPPORTED_MEDIA,
                "audio or video decoding validation failed",
            )
        duration = _float_or_none(metadata.get("duration"))
        if duration is not None and duration > request.max_video_duration_seconds:
            raise AnalyzerError(
                ErrorCode.LIMIT_EXCEEDED,
                "media duration exceeds the configured analysis limit",
            )
        width = _int_or_none(metadata.get("width"))
        height = _int_or_none(metadata.get("height"))
        warnings: list[str] = []
        prepared_images: list[Path] = []
        ocr_text: list[str] = []
        evidence: list[Evidence] = []

        if modality == "video":
            prepared_images, frame_warning = self._extract_keyframes(
                path,
                work_directory / f"frames-{artifact_index:03d}",
                duration,
                request.max_keyframes,
            )
            if frame_warning:
                warnings.append(frame_warning)
            if request.run_ocr:
                if self._ocr.name == "ocr-disabled":
                    warnings.append("OCR is not installed or has been disabled.")
                else:
                    for frame in prepared_images:
                        try:
                            ocr_text.extend(self._ocr.extract(frame))
                        except Exception as exc:
                            record_exception("media-content", "analysis.video_ocr", exc)
                            warnings.append(
                                "Video-frame OCR failed "
                                f"({_exception_summary(exc)})."
                            )
                            break
            ocr_text = _unique(ocr_text)
            for number, text in enumerate(ocr_text, start=1):
                evidence.append(
                    Evidence(
                        evidence_id=f"ocr-{artifact_index}-{number}",
                        kind="ocr",
                        artifact_sha256=sha256,
                        text=text,
                        confidence=0.8,
                    )
                )

        transcript: list[TranscriptSegment] = []
        if request.transcribe_audio:
            if self._transcriber.name == "asr-disabled":
                warnings.append("Speech recognition is not installed or has been disabled.")
            else:
                audio_path = work_directory / f"audio-{artifact_index:03d}.wav"
                if self._extract_audio(path, audio_path):
                    try:
                        transcript = self._transcriber.transcribe(
                            audio_path, request.language_hint
                        )
                    except Exception as exc:
                        record_exception("media-content", "analysis.speech_recognition", exc)
                        warnings.append(
                            "Speech recognition failed "
                            f"({_exception_summary(exc)})."
                        )
                elif modality == "audio":
                    try:
                        transcript = self._transcriber.transcribe(path, request.language_hint)
                    except Exception as exc:
                        record_exception("media-content", "analysis.speech_recognition", exc)
                        warnings.append(
                            "Speech recognition failed "
                            f"({_exception_summary(exc)})."
                        )
                else:
                    warnings.append("No decodable audio track was found.")
        for number, segment in enumerate(transcript, start=1):
            evidence.append(
                Evidence(
                    evidence_id=f"transcript-{artifact_index}-{number}",
                    kind="transcript",
                    artifact_sha256=sha256,
                    text=segment.text,
                    timestamp_seconds=segment.start_seconds,
                    confidence=0.85,
                )
            )

        return (
            AssetAnalysis(
                artifact_sha256=sha256,
                media_type=media_type,
                modality=modality,  # type: ignore[arg-type]
                width=width,
                height=height,
                duration_seconds=duration,
                ocr_text=ocr_text,
                transcript=transcript,
                sampled_frame_count=len(prepared_images),
                warnings=_unique(warnings),
            ),
            evidence,
            prepared_images,
        )

    def _probe(self, path: Path) -> dict[str, Any]:
        if self._ffprobe:
            completed = subprocess.run(
                [
                    self._ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_type,width,height",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if completed.returncode == 0:
                payload = json.loads(completed.stdout or "{}")
                result: dict[str, Any] = {
                    "duration": (payload.get("format") or {}).get("duration"),
                    "valid": bool(payload.get("streams")),
                }
                for stream in payload.get("streams") or []:
                    if stream.get("codec_type") == "video":
                        result["width"] = stream.get("width")
                        result["height"] = stream.get("height")
                        break
                return result
        if not self._ffmpeg:
            return {}
        completed = subprocess.run(
            [self._ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        stderr = completed.stderr or ""
        duration_match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", stderr)
        size_match = re.search(r"Video:.*?\s(\d{2,5})x(\d{2,5})", stderr)
        result = {"valid": bool(re.search(r"Stream #|Duration:", stderr))}
        if duration_match:
            hours, minutes, seconds = duration_match.groups()
            result["duration"] = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if size_match:
            result["width"], result["height"] = map(int, size_match.groups())
        return result

    def _extract_keyframes(
        self,
        video_path: Path,
        output_directory: Path,
        duration: float | None,
        maximum: int,
    ) -> tuple[list[Path], str | None]:
        if not self._ffmpeg:
            return [], "FFmpeg is unavailable; video frames could not be extracted."
        output_directory.mkdir(parents=True, exist_ok=True)
        warning: str | None = None
        try:
            timestamps = _scene_timestamps(video_path, maximum)
        except Exception:
            timestamps = []
            warning = "PySceneDetect failed; uniform frame sampling was used."
        if not timestamps:
            timestamps = _uniform_timestamps(duration, maximum)
            if warning is None:
                warning = "PySceneDetect is unavailable; uniform frame sampling was used."
        frames: list[Path] = []
        for index, timestamp in enumerate(timestamps, start=1):
            target = output_directory / f"frame-{index:04d}.jpg"
            completed = subprocess.run(
                [
                    self._ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "3",
                    "-y",
                    str(target),
                ],
                capture_output=True,
                timeout=120,
                check=False,
            )
            if completed.returncode == 0 and target.is_file():
                _resize_image_in_place(target, (1600, 1600))
                frames.append(target)
        return frames, warning

    def _extract_audio(self, media_path: Path, target: Path) -> bool:
        if not self._ffmpeg:
            return False
        completed = subprocess.run(
            [
                self._ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(media_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(target),
            ],
            capture_output=True,
            timeout=300,
            check=False,
        )
        return completed.returncode == 0 and target.is_file() and target.stat().st_size > 44


def _find_ffmpeg() -> str | None:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _exception_summary(exc: Exception, maximum: int = 240) -> str:
    message = " ".join(str(exc).split())
    if len(message) > maximum:
        message = f"{message[: maximum - 1]}…"
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _detect_media(path: Path, declared_type: str | None) -> tuple[str, str]:
    guessed = mimetypes.guess_type(path.name)[0]
    media_type = (declared_type or guessed or "application/octet-stream").lower()
    suffix = path.suffix.lower()
    if media_type.startswith("image/") or suffix in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
    }:
        return media_type if media_type.startswith("image/") else "image/unknown", "image"
    if media_type.startswith("video/") or suffix in {
        ".mp4",
        ".mov",
        ".mkv",
        ".webm",
        ".avi",
        ".flv",
        ".m4v",
    }:
        return media_type if media_type.startswith("video/") else "video/unknown", "video"
    if media_type.startswith("audio/") or suffix in {
        ".mp3",
        ".m4a",
        ".aac",
        ".wav",
        ".flac",
        ".ogg",
        ".opus",
    }:
        return media_type if media_type.startswith("audio/") else "audio/unknown", "audio"
    return media_type, "unknown"


def _scene_timestamps(path: Path, maximum: int) -> list[float]:
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import ContentDetector

    video = open_video(str(path))
    manager = SceneManager()
    manager.add_detector(ContentDetector())
    manager.detect_scenes(video=video, show_progress=False)
    scenes = manager.get_scene_list(start_in_scene=True)
    values = [
        (start.get_seconds() + end.get_seconds()) / 2.0 for start, end in scenes
    ]
    return _evenly_sample_numbers(values, maximum)


def _uniform_timestamps(duration: float | None, maximum: int) -> list[float]:
    if not duration or duration <= 0:
        return [0.0]
    count = min(maximum, max(1, math.ceil(duration / 10.0)))
    return [duration * (index + 0.5) / count for index in range(count)]


def _evenly_sample_numbers(values: list[float], maximum: int) -> list[float]:
    if len(values) <= maximum:
        return values
    if maximum == 1:
        return [values[len(values) // 2]]
    return [values[round(index * (len(values) - 1) / (maximum - 1))] for index in range(maximum)]


def _resize_image_in_place(path: Path, maximum: tuple[int, int]) -> None:
    with Image.open(path) as image:
        normalized = ImageOps.exif_transpose(image).convert("RGB")
        normalized.thumbnail(maximum, Image.Resampling.LANCZOS)
        normalized.save(path, "JPEG", quality=86, optimize=True)


def _perceptual_hash(image: Image.Image) -> str:
    try:
        import imagehash

        return str(imagehash.phash(image))
    except ImportError:
        grayscale = image.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
        pixels = list(grayscale.tobytes())
        average = sum(pixels) / len(pixels)
        bits = "".join("1" if pixel >= average else "0" for pixel in pixels)
        return f"ahash:{int(bits, 2):016x}"


def _trusted_context(assets: Sequence[AssetAnalysis]) -> str:
    lines = []
    for index, asset in enumerate(assets, start=1):
        lines.append(
            f"asset-{index}: modality={asset.modality}, media_type={asset.media_type}, "
            f"size={asset.width or '?'}x{asset.height or '?'}, "
            f"duration={asset.duration_seconds or '?'}s, sampled_frames={asset.sampled_frame_count}"
        )
    return "\n".join(lines)


def _untrusted_context(evidence: Sequence[Evidence]) -> str:
    lines = []
    for item in evidence:
        if item.text:
            lines.append(f"[{item.evidence_id}|{item.kind}] {item.text}")
    return "\n".join(lines)[:120_000]


def _deterministic_understanding(
    *,
    request: AnalyzeContentInput,
    assets: Sequence[AssetAnalysis],
    evidence: Sequence[Evidence],
) -> SemanticResult:
    transcript_text = " ".join(
        segment.text for asset in assets for segment in asset.transcript
    ).strip()
    ocr_text = " ".join(text for asset in assets for text in asset.ocr_text).strip()
    combined = " ".join(
        part for part in (request.post_text or "", transcript_text, ocr_text) if part
    ).strip()
    summary = _compact_summary(combined)
    if not summary:
        modalities = _unique([asset.modality for asset in assets if asset.modality != "unknown"])
        summary = f"Downloaded {' and '.join(modalities)} content; no textual evidence was extracted."
    topics = _keywords(combined, maximum=8)
    claims = []
    if request.post_text:
        claim = _first_sentence(request.post_text)
        if claim:
            claims.append(claim)
    evidence_refs = [item.evidence_id for item in evidence if item.text][:20]
    modalities = {asset.modality for asset in assets}
    return SemanticResult(
        language=request.language_hint or _detect_language(combined),
        summary=summary,
        topics=topics,
        entities=[],
        claims=claims,
        image_summary=(
            _compact_summary(ocr_text) or "Image content available for visual-model analysis."
            if "image" in modalities
            else None
        ),
        video_summary=(
            _compact_summary(" ".join(part for part in (transcript_text, ocr_text) if part))
            or "Video keyframes were sampled for visual-model analysis."
            if "video" in modalities
            else None
        ),
        transcript_summary=_compact_summary(transcript_text) or None,
        sentiment="neutral",
        commercial_intent=None,
        safety_flags=[],
        confidence=0.5 if combined else 0.3,
        evidence_refs=evidence_refs,
    )


def _build_tags(
    semantic: SemanticResult,
    modalities: set[str],
    evidence_refs: list[str],
) -> list[Tag]:
    tags: list[Tag] = []
    bounded_refs = _unique(evidence_refs)[:TAG_EVIDENCE_MAX_ITEMS]

    def add(namespace: TagNamespace, label: str, *, confidence: float | None = None) -> None:
        # Semantic fields may be verbose and have no tag-length constraint.
        # Bound only their compact tag representation, not the full analysis.
        label = " ".join(label.split())
        if not label:
            return
        if len(label) > TAG_LABEL_MAX_LENGTH:
            label = label[:TAG_LABEL_MAX_LENGTH - 1].rstrip() + "…"
        tags.append(Tag(
            namespace=namespace, label=label,
            confidence=semantic.confidence if confidence is None else confidence,
            evidence_refs=[] if namespace == TagNamespace.FORMAT else bounded_refs,
        ))

    for topic in semantic.topics:
        add(TagNamespace.TOPIC, topic)
    for entity in semantic.entities:
        add(TagNamespace.ENTITY, entity)
    for modality in sorted(modalities):
        add(TagNamespace.FORMAT, modality, confidence=1.0)
    add(TagNamespace.SENTIMENT, semantic.sentiment)
    if semantic.commercial_intent:
        add(TagNamespace.COMMERCIAL, semantic.commercial_intent)
    for flag in semantic.safety_flags:
        add(TagNamespace.SAFETY, flag)
    result: list[Tag] = []
    seen: set[tuple[TagNamespace, str]] = set()
    for tag in tags:
        key = (tag.namespace, tag.label.casefold())
        if tag.label.strip() and key not in seen:
            seen.add(key)
            result.append(tag)
    return result


def _keywords(text: str, maximum: int) -> list[str]:
    if not text:
        return []
    latin = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text.lower())
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]{2,8}", text)
    stopwords = {
        "this", "that", "with", "from", "have", "http", "https",
        "一个", "这个", "我们", "你们", "他们", "可以", "进行", "以及", "内容",
    }
    counts = Counter(word for word in latin + chinese_chunks if word not in stopwords)
    return [word for word, _ in counts.most_common(maximum)]


def _compact_summary(text: str, maximum: int = 500) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= maximum:
        return normalized
    return normalized[: maximum - 1].rstrip() + "…"


def _first_sentence(text: str) -> str:
    normalized = " ".join(text.split())
    parts = re.split(r"(?<=[。！？.!?])\s*", normalized, maxsplit=1)
    return parts[0][:500] if parts else ""


def _detect_language(text: str) -> str:
    if not text:
        return "unknown"
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk > latin * 0.25:
        return "zh"
    if latin:
        return "en"
    return "unknown"


def _unique(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(str(value).split()).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None
