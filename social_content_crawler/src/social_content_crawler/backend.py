from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
from html import unescape
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import imageio_ffmpeg
import yt_dlp
from playwright.sync_api import sync_playwright
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
            "match_filter": _download_filter(request.max_duration_seconds),
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
                try:
                    info, downloader = self._extract_with_browser_fallback(
                        extractor_url,
                        request,
                        base_options,
                    )
                    safe_info = downloader.sanitize_info(info) if info else None
                except DownloadError as exc:
                    if _is_xiaohongshu_url(extractor_url) and _is_no_video_formats_error(exc):
                        info, downloader = _extract_xiaohongshu_image_post(
                            extractor_url,
                            base_options,
                        )
                        safe_info = downloader.sanitize_info(info) if info else None
                    elif not _is_douyin_url(extractor_url):
                        raise
                    else:
                        safe_info = _douyin_public_page_fallback(
                            str(source_url), request, output_directory
                        )
                if not safe_info:
                    continue
                for item in _flatten_info(safe_info):
                    safe = item
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
        or (url.host or "").lower().endswith("xhslink.cn")
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


def _douyin_public_page_fallback(
    page_url: str, request: DownloadInput, output_directory: Path
) -> dict[str, Any]:
    html, resolved_url, runtime_media_url = _render_public_page(
        page_url,
        _douyin_video_id(page_url),
        request.request_timeout_seconds,
    )
    video_id = _douyin_video_id(resolved_url) or _extract_douyin_video_id(html)
    if not video_id:
        raise DownloadError("Unable to identify the Douyin video after resolving the public page")
    media_url = (
        _validate_douyin_media_url(runtime_media_url)
        if runtime_media_url
        else _extract_douyin_play_url(html, video_id)
    )
    title_match = re.search(r'<meta name="lark:url:video_title" content="([^"]*)"', html)
    title = unescape(title_match.group(1)) if title_match else f"Douyin-{video_id}"
    if "\ufffd" in title:
        title = f"Douyin-{video_id}"

    if request.mode is DownloadMode.DOWNLOAD:
        output_path = output_directory / f"Douyin-{video_id}.mp4"
        _download_public_media(
            media_url,
            output_path,
            page_url,
            max_bytes=request.max_file_size_mb * 1024 * 1024,
            timeout=request.request_timeout_seconds,
        )
        if request.media_format is MediaFormat.AUDIO:
            ffmpeg = _ffmpeg_executable()
            if not ffmpeg:
                raise DownloadError("FFmpeg is required for audio extraction")
            audio_path = output_path.with_suffix(".m4a")
            completed = subprocess.run(
                [ffmpeg, "-nostdin", "-y", "-i", str(output_path), "-vn", "-c:a", "copy", str(audio_path)],
                capture_output=True,
                check=False,
                timeout=600,
            )
            if completed.returncode != 0 or not audio_path.is_file():
                raise DownloadError("Unable to extract audio from the Douyin video")
            output_path.unlink(missing_ok=True)

    return {
        "id": video_id,
        "extractor_key": "DouyinPublicPage",
        "webpage_url": page_url,
        "title": title,
        "__source_url": page_url,
    }


def _is_xiaohongshu_url(value: str) -> bool:
    host = (urlsplit(value).hostname or "").lower().rstrip(".")
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in ("xiaohongshu.com", "xhslink.com", "xhslink.cn")
    )


def _is_no_video_formats_error(exc: DownloadError) -> bool:
    return "no video formats found" in str(exc).lower()


def _extract_xiaohongshu_image_post(
    extractor_url: str,
    base_options: dict[str, Any],
) -> tuple[dict[str, Any] | None, yt_dlp.YoutubeDL]:
    options = dict(base_options)
    options.update(
        {
            "skip_download": True,
            "writethumbnail": True,
            "write_all_thumbnails": True,
            "ignore_no_formats_error": True,
        }
    )
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(extractor_url, download=True)
        if info and not info.get("thumbnails"):
            raise DownloadError("XiaoHongShu image post did not expose downloadable images")
        return info, downloader


def _douyin_video_id(value: str) -> str | None:
    parsed = urlsplit(value)
    modal_id = parse_qs(parsed.query).get("modal_id", [None])[0]
    if modal_id and modal_id.isdigit():
        return modal_id
    match = re.search(r"/video/(\d+)", parsed.path)
    return match.group(1) if match else None


def _extract_douyin_video_id(html: str) -> str | None:
    match = re.search(r"%22awemeId%22%3A%22(\d+)%22", html)
    return match.group(1) if match else None


def _render_public_page(
    page_url: str, video_id: str | None, timeout: float
) -> tuple[str, str, str | None]:
    timeout_ms = int(timeout * 1000)
    last_error: Exception | None = None
    with sync_playwright() as playwright:
        for executable in _chromium_executables():
            browser = None
            try:
                browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
                page = browser.new_page()
                page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
                resolved_id = video_id or _douyin_video_id(page.url)
                marker = (
                    f"%22awemeId%22%3A%22{resolved_id}%22"
                    if resolved_id
                    else "%22awemeId%22%3A%22"
                )
                page.wait_for_function(
                    "args => document.documentElement.innerHTML.includes(args.marker) || "
                    "([...document.querySelectorAll('video')].some(video => "
                    "video.currentSrc.includes('douyinvod.com') && "
                    "(!args.videoId || video.currentSrc.includes(args.videoId))))",
                    arg={"marker": marker, "videoId": resolved_id},
                    timeout=timeout_ms,
                )
                runtime_media_url = page.locator("video").evaluate_all(
                    "(videos, videoId) => videos.map(video => video.currentSrc || video.src)"
                    ".find(url => url.includes('douyinvod.com') && "
                    "(!videoId || url.includes(videoId))) || null",
                    resolved_id,
                )
                return page.content(), page.url, runtime_media_url
            except Exception as exc:
                last_error = exc
            finally:
                if browser is not None:
                    browser.close()
    raise DownloadError("Unable to render the public Douyin page") from last_error


def _chromium_executables() -> list[Path]:
    program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
        program_files / "Microsoft/Edge/Application/msedge.exe",
        local / "Microsoft/Edge/Application/msedge.exe",
        program_files / "Google/Chrome/Application/chrome.exe",
        program_files_x86 / "Google/Chrome/Application/chrome.exe",
        local / "Google/Chrome/Application/chrome.exe",
    ]
    return [path for path in candidates if path.is_file()]


def _extract_douyin_play_url(html: str, video_id: str) -> str:
    from urllib.parse import unquote

    marker = f"%22awemeId%22%3A%22{video_id}%22"
    start = html.find(marker)
    if start < 0:
        raise DownloadError("Douyin public page data was not found")
    match = re.search(
        r"%22playAddr%22%3A%5B%7B%22src%22%3A%22(.*?)%22",
        html[start : start + 300_000],
    )
    if not match:
        raise DownloadError("Douyin public media address was not found")
    return _validate_douyin_media_url(unquote(match.group(1)))


def _validate_douyin_media_url(media_url: str) -> str:
    host = (urlsplit(media_url).hostname or "").lower()
    if not media_url.startswith("https://") or not (
        host.endswith(".douyinvod.com") or host.endswith(".douyin.com")
    ):
        raise DownloadError("Douyin returned an unexpected media host")
    return media_url


def _download_public_media(
    media_url: str,
    destination: Path,
    referer: str,
    *,
    max_bytes: int,
    timeout: float,
) -> None:
    request = Request(
        media_url,
        headers={"Referer": referer, "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    temporary = destination.with_suffix(destination.suffix + ".part")
    total = 0
    try:
        with urlopen(request, timeout=timeout) as response, temporary.open("wb") as output:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise DownloadError("Douyin media exceeds the configured file size limit")
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise DownloadError("Douyin media exceeds the configured file size limit")
                output.write(chunk)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


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
    return [
        browser
        for browser, locations in candidates
        if any(location.exists() for location in locations)
    ]


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


def _download_filter(max_duration_seconds: int):
    duration_filter = _duration_filter(max_duration_seconds)

    def match(info: dict[str, Any], *, incomplete: bool = False) -> str | None:
        _keep_xiaohongshu_original_images(info)
        return duration_filter(info, incomplete=incomplete)

    return match


def _keep_xiaohongshu_original_images(info: dict[str, Any]) -> None:
    if str(info.get("extractor_key", "")).lower() != "xiaohongshu":
        return
    thumbnails = info.get("thumbnails")
    if not isinstance(thumbnails, list):
        return
    originals = [
        thumbnail
        for thumbnail in thumbnails
        if isinstance(thumbnail, dict)
        and "!nd_dft_" in str(thumbnail.get("url", "")).lower()
    ]
    if originals:
        info["thumbnails"] = originals
        info["thumbnail"] = originals[-1].get("url")


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
    lowered = message.lower()
    if "[twitter]" in lowered and "no video could be found" in lowered:
        return (
            "该 X/Twitter 帖子没有可公开下载的视频。它可能是纯文字或图片帖，"
            "也可能已删除、设为私密或需要登录权限。当前工具只下载公开可访问的内容。"
        )
    if "fresh cookies" in lowered:
        return (
            "抖音需要新鲜的浏览器站点会话。请先用 Chrome、Edge 或 Firefox 打开一次该公开作品，"
            "确认界面中的“首次或缓存失效时允许读取浏览器会话”已开启，然后重试。"
        )
    return message
