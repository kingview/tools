from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlsplit, urlunsplit

import imageio_ffmpeg
import yt_dlp
from yt_dlp.utils import DownloadError

from .contracts import BrowserCookieSource, DownloadInput, DownloadMode, MediaFormat
from .errors import CrawlerError, ErrorCode


_DOUYIN_AUTH_COOKIE_NAMES = {
    "login_time",
    "n_mh",
    "passport_assist_user",
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_tt",
    "sid_ucp_v1",
    "ssid_ucp_v1",
    "uid_tt",
    "uid_tt_ss",
    "x_tt_token",
}


class _QuietLogger:
    def debug(self, message: str) -> None:
        return None

    def warning(self, message: str) -> None:
        return None

    def error(self, message: str) -> None:
        return None


class YtDlpBackend:
    """Embedded yt-dlp with a deliberately small and non-authenticated option surface."""

    def __init__(
        self,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        cookie_cache_path: Path | None = None,
    ) -> None:
        self._progress_callback = progress_callback
        self._cookie_cache_path = (cookie_cache_path or _default_cookie_cache_path()).expanduser()

    def run(self, request: DownloadInput, output_directory: Path) -> list[dict[str, Any]]:
        download_post_images = _supports_image_posts(request)
        ffmpeg_executable = _ffmpeg_executable()
        base_options: dict[str, Any] = {
            "format": _format_selector(
                request.media_format,
                ffmpeg_available=ffmpeg_executable is not None,
            ),
            "skip_download": request.mode is DownloadMode.METADATA_ONLY,
            "noplaylist": not request.include_playlists,
            "playlistend": request.max_items,
            "max_filesize": request.max_file_size_mb * 1024 * 1024,
            "match_filter": _duration_filter(request.max_duration_seconds),
            "socket_timeout": request.request_timeout_seconds,
            "retries": 2,
            "fragment_retries": 2,
            "concurrent_fragment_downloads": 1,
            "sleep_interval_requests": 1.0,
            "writethumbnail": request.mode is DownloadMode.DOWNLOAD and (
                request.write_thumbnail or download_post_images
            ),
            "write_all_thumbnails": request.mode is DownloadMode.DOWNLOAD and download_post_images,
            "writesubtitles": request.mode is DownloadMode.DOWNLOAD and request.write_subtitles,
            "writeautomaticsub": False,
            "ignore_no_formats_error": download_post_images,
            "outtmpl": str(output_directory / "%(extractor_key)s-%(id)s.%(ext)s"),
            "restrictfilenames": True,
            "overwrites": False,
            "continuedl": True,
            "quiet": True,
            "no_warnings": True,
            "logger": _QuietLogger(),
            "cachedir": False,
        }
        if ffmpeg_executable is not None:
            base_options["ffmpeg_location"] = ffmpeg_executable
        if self._progress_callback is not None:
            base_options["progress_hooks"] = [self._progress_callback]
        collected: list[dict[str, Any]] = []
        try:
            for source_url in request.urls:
                if len(collected) >= request.max_items:
                    break
                extractor_url = normalize_extractor_url(str(source_url))
                info, downloader = self._extract_with_browser_fallback(
                    extractor_url,
                    request,
                    base_options,
                )
                if not info:
                    continue
                for item in _flatten_info(info):
                    safe = downloader.sanitize_info(item)
                    if isinstance(safe, dict):
                        safe["__source_url"] = str(source_url)
                        collected.append(safe)
                        if len(collected) >= request.max_items:
                            break
            if request.mode is DownloadMode.DOWNLOAD and request.media_format is MediaFormat.VIDEO:
                if ffmpeg_executable is None:
                    raise CrawlerError(
                        ErrorCode.CONFIGURATION_ERROR,
                        "仅视频模式需要 FFmpeg，但当前运行环境中没有找到 FFmpeg。",
                    )
                _strip_audio_tracks(output_directory, ffmpeg_executable)
        except DownloadError as exc:
            message = _download_error_message(exc)
            code = ErrorCode.UNSUPPORTED_URL if "unsupported url" in message.lower() else ErrorCode.DOWNLOAD_FAILED
            raise CrawlerError(code, message, retryable=False) from exc
        return collected

    def _extract_with_browser_fallback(
        self,
        extractor_url: str,
        request: DownloadInput,
        base_options: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, yt_dlp.YoutubeDL]:
        candidates: list[str | None] = [None]
        may_use_session = (
            _is_douyin_url(extractor_url)
            and request.browser_cookie_source is not BrowserCookieSource.NONE
        )
        if may_use_session and self._cookie_cache_path.is_file():
            candidates.append("__cache__")
        if may_use_session:
            candidates.extend(_browser_cookie_candidates(request.browser_cookie_source))

        last_error: DownloadError | None = None
        for index, browser in enumerate(candidates):
            options = dict(base_options)
            if browser == "__cache__":
                options["cookiefile"] = str(self._cookie_cache_path)
            elif browser:
                options["cookiesfrombrowser"] = (browser, None, None, None)
            with yt_dlp.YoutubeDL(options) as downloader:
                try:
                    info = downloader.extract_info(
                        extractor_url,
                        download=request.mode is DownloadMode.DOWNLOAD,
                    )
                    if browser and browser != "__cache__":
                        _save_douyin_cookie_cache(
                            downloader.cookiejar,
                            self._cookie_cache_path,
                        )
                    return info, downloader
                except DownloadError as exc:
                    last_error = exc
                    needs_fresh_cookies = "fresh cookies" in str(exc).lower()
                    has_another_candidate = index < len(candidates) - 1
                    if has_another_candidate and (needs_fresh_cookies or browser is not None):
                        continue
                    raise
        assert last_error is not None
        raise last_error


def _format_selector(media_format: MediaFormat, *, ffmpeg_available: bool) -> str:
    if media_format is MediaFormat.AUDIO:
        return "bestaudio/best"
    if media_format is MediaFormat.VIDEO:
        return "bestvideo[ext=mp4][vcodec^=avc1]/bestvideo[ext=mp4]/bestvideo/best"
    if ffmpeg_available:
        return (
            "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        )
    return "best[ext=mp4]/best/bestvideo[ext=mp4]/bestvideo"


def _ffmpeg_executable() -> str | None:
    try:
        bundled = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if bundled.is_file():
            return str(bundled)
    except (OSError, RuntimeError):
        pass
    return shutil.which("ffmpeg")


def _strip_audio_tracks(output_directory: Path, ffmpeg_executable: str) -> None:
    video_suffixes = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
    video_files = [
        path
        for path in output_directory.rglob("*")
        if path.is_file() and path.suffix.lower() in video_suffixes
    ]
    if not video_files:
        raise CrawlerError(
            ErrorCode.DOWNLOAD_FAILED,
            "该帖子没有产生可用的视频文件。",
        )

    for path in video_files:
        temporary = path.with_name(
            f".{path.stem}.video-only-{uuid.uuid4().hex[:8]}{path.suffix}"
        )
        command = [
            ffmpeg_executable,
            "-nostdin",
            "-y",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map_metadata",
            "0",
            "-c:v",
            "copy",
            "-an",
            str(temporary),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            temporary.unlink(missing_ok=True)
            raise CrawlerError(
                ErrorCode.DOWNLOAD_FAILED,
                "无法生成仅视频文件。",
            ) from exc
        if completed.returncode != 0 or not temporary.is_file():
            temporary.unlink(missing_ok=True)
            raise CrawlerError(
                ErrorCode.DOWNLOAD_FAILED,
                "无法移除视频中的音轨。",
            )
        temporary.replace(path)


def _supports_image_posts(request: DownloadInput) -> bool:
    return any(
        (url.host or "").lower().endswith("xiaohongshu.com")
        or (url.host or "").lower().endswith("xhslink.com")
        for url in request.urls
    )


def normalize_extractor_url(value: str) -> str:
    """Convert alternate public post routes into URLs recognized by extractors."""

    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host == "douyin.com" or host.endswith(".douyin.com"):
        modal_ids = parse_qs(parsed.query).get("modal_id", [])
        if modal_ids and modal_ids[0].isdigit():
            return urlunsplit(("https", "www.douyin.com", f"/video/{modal_ids[0]}", "", ""))
    return value


def _is_douyin_url(value: str) -> bool:
    host = (urlsplit(value).hostname or "").lower().rstrip(".")
    return host == "douyin.com" or host.endswith(".douyin.com")


def _browser_cookie_candidates(source: BrowserCookieSource) -> list[str]:
    if source is BrowserCookieSource.NONE:
        return []
    if source is not BrowserCookieSource.AUTO:
        return [source.value]

    home = Path.home()
    candidates: list[tuple[str, tuple[Path, ...]]] = []
    if sys.platform == "darwin":
        candidates = [
            ("chrome", (home / "Library/Application Support/Google/Chrome",)),
            ("edge", (home / "Library/Application Support/Microsoft Edge",)),
            ("firefox", (home / "Library/Application Support/Firefox",)),
            ("safari", (home / "Library/Cookies/Cookies.binarycookies",)),
        ]
    elif sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        roaming = Path(os.environ.get("APPDATA", ""))
        candidates = [
            ("chrome", (local / "Google/Chrome/User Data",)),
            ("edge", (local / "Microsoft/Edge/User Data",)),
            ("firefox", (roaming / "Mozilla/Firefox",)),
        ]
    else:
        candidates = [
            ("chrome", (home / ".config/google-chrome", home / ".config/chromium")),
            ("edge", (home / ".config/microsoft-edge",)),
            ("firefox", (home / ".mozilla/firefox",)),
        ]
    for browser, locations in candidates:
        if any(location.exists() for location in locations):
            return [browser]
    return []


def _default_cookie_cache_path() -> Path:
    configured = os.environ.get("SOCIAL_DOWNLOADER_COOKIE_CACHE")
    if configured:
        return Path(configured)
    if sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/PostDrop"
    elif sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "PostDrop"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "PostDrop"
    return root / "sessions/douyin.cookies.txt"


def _save_douyin_cookie_cache(source: Iterable[Cookie], destination: Path) -> None:
    jar = MozillaCookieJar(str(destination))
    for cookie in source:
        domain = cookie.domain.lower().lstrip(".")
        is_douyin = domain == "douyin.com" or domain.endswith(".douyin.com")
        is_auth = cookie.name in _DOUYIN_AUTH_COOKIE_NAMES or cookie.name.startswith("passport_")
        if is_douyin and not is_auth:
            jar.set_cookie(cookie)
    if not list(jar):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.parent.chmod(0o700)
    except OSError:
        pass
    jar.save(ignore_discard=True, ignore_expires=True)
    try:
        destination.chmod(0o600)
    except OSError:
        pass


def _duration_filter(max_duration_seconds: int):
    def match(info: dict[str, Any], *, incomplete: bool = False) -> str | None:
        duration = info.get("duration")
        if isinstance(duration, (int, float)) and duration > max_duration_seconds:
            return f"media duration exceeds {max_duration_seconds} seconds"
        return None

    return match


def _flatten_info(info: dict[str, Any]) -> Iterable[dict[str, Any]]:
    entries = info.get("entries")
    if isinstance(entries, (list, tuple)):
        for entry in entries:
            if isinstance(entry, dict):
                yield from _flatten_info(entry)
        return
    yield info


def _redact_urls(message: str) -> str:
    return re.sub(r"https?://\S+", "[url]", message)


def _download_error_message(exc: DownloadError) -> str:
    message = _redact_urls(str(exc))[:500]
    if "fresh cookies" in message.lower():
        return (
            "抖音需要新鲜的浏览器站点会话。请先用 Chrome、Edge 或 Firefox 打开一次该公开作品，"
            "确认界面中的“首次或缓存失效时允许读取浏览器会话”已开启，然后重试。"
        )
    return message
