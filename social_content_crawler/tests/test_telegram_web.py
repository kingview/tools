from __future__ import annotations

import pytest

from social_content_crawler.errors import CrawlerError, ErrorCode
from social_content_crawler.telegram_web import (
    _append_channel_checkpoint,
    _download_browser_media,
    _read_channel_checkpoint,
    _response_total,
    parse_telegram_channel_url,
    parse_telegram_post_url,
    telegram_message_url,
)


def test_parses_public_and_private_telegram_post_urls() -> None:
    assert parse_telegram_post_url("https://t.me/channel_name/123") == (
        "channel_name",
        "123",
        "https://t.me/channel_name/123",
        "https://web.telegram.org/a/#@channel_name",
    )
    assert parse_telegram_post_url("https://t.me/c/1634371164/456") == (
        "1634371164",
        "456",
        "https://t.me/c/1634371164/456",
        "https://web.telegram.org/a/#-1001634371164",
    )


def test_rejects_channel_url_without_message_id_for_download() -> None:
    with pytest.raises(CrawlerError) as raised:
        parse_telegram_post_url("https://t.me/channel_name")
    assert raised.value.code is ErrorCode.INVALID_REQUEST


def test_parses_channel_urls_and_builds_message_urls() -> None:
    assert parse_telegram_channel_url("https://t.me/weme_download") == (
        "weme_download",
        "https://t.me/weme_download",
        "https://web.telegram.org/a/#@weme_download",
    )
    assert parse_telegram_channel_url("https://t.me/c/1634371164") == (
        "1634371164",
        "https://t.me/c/1634371164",
        "https://web.telegram.org/a/#-1001634371164",
    )
    assert telegram_message_url("weme_download", "123") == "https://t.me/weme_download/123"
    assert telegram_message_url("1634371164", "456") == "https://t.me/c/1634371164/456"


def test_channel_checkpoint_ignores_truncated_last_line(tmp_path) -> None:
    checkpoint = tmp_path / "telegram-channel-manifest.jsonl"
    _append_channel_checkpoint(
        checkpoint,
        {"id": "123", "__source_url": "https://t.me/weme_download/123", "files": ["a.jpg"]},
    )
    with checkpoint.open("a", encoding="utf-8") as stream:
        stream.write('{"id":"122"')
    _append_channel_checkpoint(
        checkpoint,
        {"id": "121", "__source_url": "https://t.me/weme_download/121", "files": []},
    )
    assert _read_channel_checkpoint(checkpoint) == {
        "123": {"id": "123", "__source_url": "https://t.me/weme_download/123"},
        "121": {"id": "121", "__source_url": "https://t.me/weme_download/121"},
    }


def test_reads_total_bytes_from_range_response() -> None:
    assert _response_total({"contentRange": "bytes 0-524287/1054815354"}) == 1_054_815_354
    assert _response_total({"contentLength": "1234"}) == 1_234


def test_downloads_browser_blob_image_to_disk(tmp_path) -> None:
    class FakePage:
        def evaluate(self, script, arguments):
            assert arguments["start"] is None
            return {
                "data": "aW1hZ2UtYnl0ZXM=",
                "mime": "image/png",
                "contentRange": "",
                "contentLength": "11",
            }

    path, size = _download_browser_media(
        FakePage(),
        "blob:https://web.telegram.org/example",
        "image",
        tmp_path / "message-1",
        maximum_file_bytes=1024,
        maximum_total_bytes=1024,
        progress_callback=None,
    )

    assert path.name == "message-1.png"
    assert path.read_bytes() == b"image-bytes"
    assert size == 11
