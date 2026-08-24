from media_content_analyzer.adapters import (
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_OLLAMA_MODEL,
    OpenAICompatibleVisionModel,
    _normalize_copy_variants,
    _collect_ocr_text,
    _coerce_confidence,
    _coerce_string,
    _coerce_string_list,
    _normalize_openai_base_url,
    _output_language_instruction,
    _resolve_whisper_model,
    _strip_json_fence,
)


def test_collect_ocr_text_supports_legacy_shape() -> None:
    value = [[[[0, 0], [1, 0], [1, 1], [0, 1]], ("你好", 0.99)]]
    assert _collect_ocr_text(value) == ["你好"]


def test_collect_ocr_text_supports_new_shape() -> None:
    assert _collect_ocr_text({"res": {"rec_texts": ["第一行", "第二行"]}}) == [
        "第一行",
        "第二行",
    ]


def test_strip_json_fence() -> None:
    assert _strip_json_fence("```json\n{\"ok\": true}\n```") == '{"ok": true}'


def test_openai_base_url_adds_v1_path() -> None:
    assert _normalize_openai_base_url("http://127.0.0.1:11434") == DEFAULT_OLLAMA_BASE_URL
    assert _normalize_openai_base_url(f"{DEFAULT_OLLAMA_BASE_URL}/") == (
        DEFAULT_OLLAMA_BASE_URL
    )


def test_model_defaults_to_local_ollama(monkeypatch) -> None:
    monkeypatch.delenv("CONTENT_ANALYZER_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("CONTENT_ANALYZER_MODEL", raising=False)

    model = OpenAICompatibleVisionModel.from_environment()

    assert model is not None
    assert model.name == f"openai-compatible:{DEFAULT_OLLAMA_MODEL}"
    assert model._base_url == DEFAULT_OLLAMA_BASE_URL


def test_empty_model_url_explicitly_disables_default(monkeypatch) -> None:
    monkeypatch.setenv("CONTENT_ANALYZER_MODEL_BASE_URL", "")

    assert OpenAICompatibleVisionModel.from_environment() is None


def test_whisper_huggingface_source_passes_model_name_through(monkeypatch) -> None:
    monkeypatch.setenv("CONTENT_ANALYZER_ASR_MODEL_SOURCE", "huggingface")

    assert _resolve_whisper_model("small") == "small"


def test_whisper_explicit_path_does_not_download(tmp_path, monkeypatch) -> None:
    model_path = tmp_path / "whisper"
    model_path.mkdir()
    monkeypatch.setenv("CONTENT_ANALYZER_ASR_MODEL_SOURCE", "invalid")

    assert _resolve_whisper_model(str(model_path)) == str(model_path)


def test_semantic_string_lists_accept_common_model_object_shape() -> None:
    assert _coerce_string_list(
        [
            {"claim": "画面中有两个人", "evidence_id": "frame-1"},
            {"evidence_id": "ocr-1-2"},
            "纯字符串",
        ]
    ) == ["画面中有两个人", "ocr-1-2", "纯字符串"]


def test_semantic_scalar_strings_accept_model_list_or_object_shape() -> None:
    assert _coerce_string(["悲伤", "宿命", "唯美"]) == "悲伤"
    assert _coerce_string({"intent": "非商业内容", "confidence": 0.9}) == (
        "非商业内容"
    )


def test_confidence_accepts_qualitative_percent_and_object_shapes() -> None:
    assert _coerce_confidence("高") == 0.9
    assert _coerce_confidence("85%") == 0.85
    assert _coerce_confidence("75") == 0.75
    assert _coerce_confidence({"level": "中"}) == 0.6
    assert _coerce_confidence("unrecognized") == 0.5


def test_chinese_language_hint_requires_simplified_chinese_output() -> None:
    instruction = _output_language_instruction("zh-CN")

    assert "language to 'zh'" in instruction
    assert "Simplified Chinese" in instruction


def test_copy_variant_normalization_accepts_common_model_shapes() -> None:
    values = _normalize_copy_variants(
        [
            {"title": "标题", "copy": "正文", "hashtags": ["#旅行"]},
            "第二条正文",
        ],
        limit=2,
        max_characters=100,
        include_hashtags=True,
    )

    assert [item.body for item in values] == ["正文", "第二条正文"]
    assert values[0].hashtags == ["旅行"]
