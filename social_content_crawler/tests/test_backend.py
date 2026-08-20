from __future__ import annotations

from http.cookiejar import Cookie
from pathlib import Path
from stat import S_IMODE
from types import SimpleNamespace

from yt_dlp.utils import DownloadError

from social_content_crawler.backend import (
    YtDlpBackend,
    _format_selector,
    _strip_audio_tracks,
    normalize_extractor_url,
)
from social_content_crawler.contracts import BrowserCookieSource, DownloadInput, MediaFormat


def test_backend_uses_embedded_ytdlp_without_auth_options(
    monkeypatch, tmp_path: Path
) -> None:
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def extract_info(self, url, download):
            assert captured["match_filter"]({"duration": 1}) is None
            return {"id": "1", "extractor_key": "Test", "webpage_url": url}

        def sanitize_info(self, info):
            return dict(info)

    monkeypatch.setattr("social_content_crawler.backend.yt_dlp.YoutubeDL", FakeYoutubeDL)
    items = YtDlpBackend().run(
        DownloadInput(
            urls=["https://video.example.com/post/1"],
            mode="metadata_only",
        ),
        tmp_path,
    )

    assert items[0]["id"] == "1"
    assert captured["skip_download"] is True
    assert captured["writethumbnail"] is False
    assert captured["writesubtitles"] is False
    assert "+bestaudio" in captured["format"]
    assert Path(captured["ffmpeg_location"]).is_file()
    assert "cookiefile" not in captured
    assert "cookiesfrombrowser" not in captured
    assert "proxy" not in captured


def test_backend_registers_optional_progress_hook(monkeypatch, tmp_path: Path) -> None:
    captured = {}
    events = []

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def extract_info(self, url, download):
            captured["progress_hooks"][0]({"status": "downloading"})
            return {"id": "1", "extractor_key": "Test", "webpage_url": url}

        def sanitize_info(self, info):
            return dict(info)

    monkeypatch.setattr("social_content_crawler.backend.yt_dlp.YoutubeDL", FakeYoutubeDL)
    YtDlpBackend(progress_callback=events.append).run(
        DownloadInput(urls=["https://video.example.com/post/1"], mode="metadata_only"),
        tmp_path,
    )
    assert events == [{"status": "downloading"}]


def test_xiaohongshu_enables_image_post_download(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def extract_info(self, url, download):
            return {"id": "note-1", "extractor_key": "XiaoHongShu", "webpage_url": url}

        def sanitize_info(self, info):
            return dict(info)

    monkeypatch.setattr("social_content_crawler.backend.yt_dlp.YoutubeDL", FakeYoutubeDL)
    YtDlpBackend().run(
        DownloadInput(urls=["https://www.xiaohongshu.com/explore/abc123"]),
        tmp_path,
    )
    assert captured["writethumbnail"] is True
    assert captured["write_all_thumbnails"] is True
    assert captured["ignore_no_formats_error"] is True


def test_normalizes_douyin_jingxuan_modal_url() -> None:
    assert normalize_extractor_url(
        "https://www.douyin.com/jingxuan?modal_id=7671972586343009588"
    ) == "https://www.douyin.com/video/7671972586343009588"


def test_media_format_selectors_have_distinct_semantics() -> None:
    audio_video = _format_selector(MediaFormat.BEST, ffmpeg_available=True)
    video_only = _format_selector(MediaFormat.VIDEO, ffmpeg_available=True)
    audio_only = _format_selector(MediaFormat.AUDIO, ffmpeg_available=True)

    assert "+bestaudio" in audio_video
    assert "+bestaudio" not in video_only
    assert video_only.startswith("bestvideo")
    assert audio_only == "bestaudio/best"


def test_video_only_postprocess_removes_audio_track(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video-with-audio")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"video-only")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("social_content_crawler.backend.subprocess.run", fake_run)
    _strip_audio_tracks(tmp_path, "/bundled/ffmpeg")

    assert source.read_bytes() == b"video-only"
    assert "-an" in commands[0]
    assert commands[0][commands[0].index("-map") + 1] == "0:v:0"


def test_douyin_retries_with_local_browser_session(monkeypatch, tmp_path: Path) -> None:
    original_url = "https://www.douyin.com/jingxuan?modal_id=7671972586343009588"
    attempts = []
    cookie_cache = tmp_path / "sessions/douyin.cookies.txt"

    def cookie(domain: str, name: str) -> Cookie:
        return Cookie(
            version=0,
            name=name,
            value="test-value",
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options
            self.cookiejar = [
                cookie(".douyin.com", "douyin-session"),
                cookie(".douyin.com", "sessionid"),
                cookie(".example.com", "unrelated-session"),
            ]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def extract_info(self, url, download):
            attempts.append(
                (
                    url,
                    self.options.get("cookiesfrombrowser"),
                    self.options.get("cookiefile"),
                )
            )
            if "cookiesfrombrowser" not in self.options and "cookiefile" not in self.options:
                raise DownloadError("Fresh cookies (not necessarily logged in) are needed")
            return {"id": "7671972586343009588", "extractor_key": "Douyin", "webpage_url": url}

        def sanitize_info(self, info):
            return dict(info)

    monkeypatch.setattr("social_content_crawler.backend.yt_dlp.YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(
        "social_content_crawler.backend._browser_cookie_candidates",
        lambda source: ["chrome"],
    )

    backend = YtDlpBackend(cookie_cache_path=cookie_cache)
    items = backend.run(
        DownloadInput(
            urls=[original_url],
            mode="metadata_only",
            browser_cookie_source=BrowserCookieSource.AUTO,
        ),
        tmp_path,
    )

    canonical_url = "https://www.douyin.com/video/7671972586343009588"
    assert attempts == [
        (canonical_url, None, None),
        (canonical_url, ("chrome", None, None, None), None),
    ]
    assert items[0]["__source_url"] == original_url
    cached_text = cookie_cache.read_text()
    assert ".douyin.com" in cached_text
    assert "douyin-session" in cached_text
    assert "sessionid" not in cached_text
    assert ".example.com" not in cached_text
    assert S_IMODE(cookie_cache.stat().st_mode) == 0o600

    attempts.clear()
    backend.run(
        DownloadInput(
            urls=[original_url],
            mode="metadata_only",
            browser_cookie_source=BrowserCookieSource.AUTO,
        ),
        tmp_path,
    )
    assert attempts == [
        (canonical_url, None, None),
        (canonical_url, None, str(cookie_cache)),
    ]
