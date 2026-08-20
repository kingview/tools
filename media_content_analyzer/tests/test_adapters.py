from media_content_analyzer.adapters import _collect_ocr_text, _strip_json_fence


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
