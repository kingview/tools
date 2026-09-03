from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from media_content_analyzer import AnalyzeContentInput, ArtifactRef
from media_content_analyzer.pipeline import LocalMediaAnalysisBackend
from media_content_analyzer.ports import SemanticResult


class FakeOcr:
    name = "fake-ocr"

    def extract(self, image_path: Path) -> list[str]:
        return ["新品发布会", "上海"]


class FakeTranscriber:
    name = "fake-asr"

    def transcribe(self, audio_path: Path, language_hint: str | None):
        return []


class FakeVision:
    name = "fake-qwen3-vl"

    def understand(self, *, images, trusted_context, untrusted_content, language_hint):
        assert images
        assert "新品发布会" in untrusted_content
        return SemanticResult(
            language="zh",
            summary="品牌在上海举行新品发布会。",
            topics=["产品发布"],
            entities=["上海"],
            claims=["现场发布了一款新产品"],
            image_summary="发布会舞台和产品海报",
            video_summary=None,
            transcript_summary=None,
            sentiment="positive",
            commercial_intent="product_promotion",
            safety_flags=[],
            confidence=0.91,
            evidence_refs=["post-text-1", "ocr-1-1"],
        )


class FailingVision:
    name = "openai-compatible:qwen3.5:9b"

    def understand(self, *, images, trusted_context, untrusted_content, language_hint):
        raise ConnectionError("Ollama is unavailable")


@pytest.mark.parametrize("reference_count", [49, 50, 173])
def test_dense_image_model_references_fit_tag_contract_without_losing_evidence(tmp_path, reference_count):
    class DenseOcr:
        name = "dense-ocr"

        def extract(self, image_path):
            return [f"产品部件 {index}" for index in range(173)]

    class DenseVision(FakeVision):
        def understand(self, **kwargs):
            # Use the same valid model contract, but reference a dense product
            # diagram like a real OCR-heavy image instead of a two-line fixture.
            base = super().understand(**{**kwargs, "untrusted_content": "新品发布会"})
            return replace(base, evidence_refs=[f"ocr-1-{i}" for i in range(1, reference_count + 1)])

    image_path = tmp_path / "diagram.jpg"
    Image.new("RGB", (100, 150), color="white").save(image_path)
    backend = LocalMediaAnalysisBackend(ocr_engine=DenseOcr(), transcriber=FakeTranscriber(), vision_model=DenseVision())
    result = backend.analyze(_request(image_path, post_text=None), [image_path], tmp_path / "work")
    assert result.summary == "品牌在上海举行新品发布会。"
    assert len(result.assets[0].ocr_text) == 173
    assert len(result.evidence) == 174  # All OCR plus visual-model evidence remain available.
    evidence_ids = {item.evidence_id for item in result.evidence}
    for tag in result.tags:
        assert len(tag.evidence_refs) <= 50
        assert set(tag.evidence_refs) <= evidence_ids
        if tag.namespace.value != "format":
            assert "visual-model-1" in tag.evidence_refs


def test_verbose_and_blank_model_tags_preserve_full_semantic_fields(tmp_path):
    long_text = "模块化机器人结构与视觉功能说明" * 30

    class VerboseVision(FakeVision):
        def understand(self, **kwargs):
            return replace(super().understand(**kwargs),
                topics=["", "  ", long_text, long_text], entities=[long_text],
                sentiment=long_text, commercial_intent=long_text, safety_flags=[long_text],
                evidence_refs=["ocr-1-1"] * 60 + ["invented", "visual-model-1"])

    image_path = tmp_path / "verbose.png"
    Image.new("RGB", (100, 150), color="white").save(image_path)
    backend = LocalMediaAnalysisBackend(ocr_engine=FakeOcr(), transcriber=FakeTranscriber(), vision_model=VerboseVision())
    result = backend.analyze(_request(image_path), [image_path], tmp_path / "work")
    assert result.commercial_intent == long_text
    assert result.entities == [long_text]
    assert len([tag for tag in result.tags if tag.namespace.value == "topic"]) == 1
    for tag in result.tags:
        assert 1 <= len(tag.label) <= 200
        assert tag.evidence_refs == ([] if tag.namespace.value == "format" else ["visual-model-1", "ocr-1-1"])


def _request(path: Path, **changes) -> AnalyzeContentInput:
    data = path.read_bytes()
    values = {
        "artifacts": [
            ArtifactRef(
                path=str(path),
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                media_type="image/png",
            )
        ],
        "post_text": "这是今天的新品发布会",
    }
    values.update(changes)
    return AnalyzeContentInput(**values)


def test_image_pipeline_combines_ocr_and_semantic_model(tmp_path: Path) -> None:
    image_path = tmp_path / "post.png"
    Image.new("RGB", (640, 480), color=(20, 40, 80)).save(image_path)
    backend = LocalMediaAnalysisBackend(
        ocr_engine=FakeOcr(),
        transcriber=FakeTranscriber(),
        vision_model=FakeVision(),
        ffmpeg_path="/missing/ffmpeg",
    )

    result = backend.analyze(_request(image_path), [image_path], tmp_path / "work")

    assert result.summary == "品牌在上海举行新品发布会。"
    assert result.topics == ["产品发布"]
    assert result.entities == ["上海"]
    assert result.assets[0].ocr_text == ["新品发布会", "上海"]
    assert result.assets[0].width == 640
    assert result.assets[0].height == 480
    assert result.needs_human_review is False
    assert {(tag.namespace.value, tag.label) for tag in result.tags} >= {
        ("topic", "产品发布"),
        ("entity", "上海"),
        ("format", "image"),
        ("commercial", "product_promotion"),
    }


def test_image_pipeline_has_deterministic_fallback(tmp_path: Path) -> None:
    from media_content_analyzer.adapters import NoopVisionModel

    image_path = tmp_path / "post.png"
    Image.new("RGB", (40, 30), color="white").save(image_path)
    backend = LocalMediaAnalysisBackend(
        ocr_engine=FakeOcr(),
        transcriber=FakeTranscriber(),
        vision_model=NoopVisionModel(),
        ffmpeg_path="/missing/ffmpeg",
    )

    result = backend.analyze(_request(image_path), [image_path], tmp_path / "work")

    assert "新品发布会" in result.summary
    assert result.confidence == 0.5
    assert result.needs_human_review is True
    assert any("Semantic model is not configured" in item for item in result.warnings)


def test_image_pipeline_falls_back_when_ollama_is_unavailable(tmp_path: Path) -> None:
    image_path = tmp_path / "post.png"
    Image.new("RGB", (40, 30), color="white").save(image_path)
    backend = LocalMediaAnalysisBackend(
        ocr_engine=FakeOcr(),
        transcriber=FakeTranscriber(),
        vision_model=FailingVision(),
        ffmpeg_path="/missing/ffmpeg",
    )

    result = backend.analyze(_request(image_path), [image_path], tmp_path / "work")

    assert "新品发布会" in result.summary
    assert result.confidence == 0.5
    assert result.needs_human_review is True
    assert any("Semantic model failed" in item for item in result.warnings)
    assert result.model_versions["content_understander"] == (
        "openai-compatible:qwen3.5:9b"
    )
