from __future__ import annotations

import pytest

import social_content_crawler.telegram_web as telegram_module
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


def test_resolve_channel_searches_when_matching_title_is_only_hidden(monkeypatch) -> None:
    class Candidate:
        def __init__(self, page) -> None:
            self.page = page

        def is_visible(self, *, timeout: int) -> bool:
            return self.page.searched

        def click(self) -> None:
            self.page.url = "https://web.telegram.org/a/#-1001634371164"

    class Candidates:
        def __init__(self, page) -> None:
            self.page = page

        def count(self) -> int:
            return 1

        def nth(self, index: int) -> Candidate:
            return Candidate(self.page)

    class Search:
        def __init__(self, page) -> None:
            self.page = page
            self.first = self

        def count(self) -> int:
            return 1

        def is_visible(self, *, timeout: int) -> bool:
            return True

        def fill(self, value: str) -> None:
            assert value == "weme_download"
            self.page.searched = True

    class Page:
        url = "https://web.telegram.org/a/"
        searched = False

        def get_by_text(self, text: str, *, exact: bool) -> Candidates:
            return Candidates(self)

        def locator(self, selector: str) -> Search:
            assert selector == 'input[placeholder="Search"]'
            return Search(self)

        def wait_for_timeout(self, timeout: int) -> None:
            pass

        def wait_for_selector(self, selector: str, *, state: str, timeout: int) -> None:
            assert selector == ".MessageList"

    page = Page()
    monkeypatch.setattr(
        telegram_module,
        "_telegram_peer_from_local_state",
        lambda page, **kwargs: {
            "peer_id": "-1001634371164",
            "title": "Example channel",
        },
    )

    result = telegram_module.resolve_telegram_web_url(
        page, "https://web.telegram.org/a/#@weme_download"
    )

    assert page.searched is True
    assert result == "https://web.telegram.org/a/#-1001634371164"


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
                # Chromium may preserve an unrelated backing response length
                # on Telegram blob images; the complete decoded body wins.
                "contentLength": str(1006 * 1024 * 1024),
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


def test_video_resumes_partial_and_does_not_duplicate_prefix(tmp_path):
    import base64
    target = tmp_path/'video'
    target.with_suffix('.mp4.part').write_bytes(b'abcd')
    starts = []
    class Page:
        def evaluate(self, script, arguments):
            start = arguments['start']
            starts.append(start)
            chunk = b'abcd' if start == 0 else b'efgh'
            return dict(data=base64.b64encode(chunk).decode(),mime='video/mp4',
                        contentRange=f'bytes {start}-{start+3}/8')
    path, size = _download_browser_media(Page(),'blob:example','video',target,
        maximum_file_bytes=100,maximum_total_bytes=100,progress_callback=None)
    assert path.read_bytes() == b'abcdefgh'
    assert starts == [0, 4] and size == 8


def test_completed_video_is_reused(tmp_path):
    target = tmp_path/'video'
    target.with_suffix('.mp4').write_bytes(b'abcd')
    class Page:
        def evaluate(self, script, arguments):
            return dict(data='YWI=',mime='video/mp4',contentRange='bytes 0-1/4')
    path, _ = _download_browser_media(Page(),'blob:example','video',target,
        maximum_file_bytes=100,maximum_total_bytes=100,progress_callback=None)
    assert path.read_bytes() == b'abcd'


def test_failed_video_keeps_partial_file(tmp_path):
    class Page:
        def evaluate(self, script, arguments):
            if arguments['start']:
                raise RuntimeError('network stopped')
            return dict(data='YWI=',mime='video/mp4',contentRange='bytes 0-1/4')
    with pytest.raises(RuntimeError):
        _download_browser_media(Page(),'blob:example','video',tmp_path/'video',
            maximum_file_bytes=100,maximum_total_bytes=100,progress_callback=None)
    assert (tmp_path/'video.mp4.part').read_bytes() == b'ab'
