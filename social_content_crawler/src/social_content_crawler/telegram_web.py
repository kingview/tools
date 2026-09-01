from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .contracts import DownloadInput, DownloadMode, MediaFormat, TelegramDownloadScope
from .errors import CrawlerError, ErrorCode


_PUBLIC_POST = re.compile(r"^/([A-Za-z0-9_]{4,})/(\d+)")
_PRIVATE_POST = re.compile(r"^/c/(\d+)/(\d+)")
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)
_FETCH_CHUNK_BYTES = 512 * 1024


def resolve_telegram_web_url(page: Page, value: str) -> str:
    """Resolve and open a peer through Telegram Web's own chat-list UI."""
    parsed = urlsplit(value)
    fragment = parsed.fragment
    if not fragment:
        return value
    handle = fragment[1:].split("/", 1)[0].strip().lower() if fragment.startswith("@") else None
    peer_id = fragment.split("_", 1)[0] if fragment.startswith("-100") else None
    peer = _telegram_peer_from_local_state(page, handle=handle, peer_id=peer_id)
    if peer is None and handle:
        search = page.locator('input[placeholder="Search"]').first
        if search.count():
            search.fill(handle)
            page.wait_for_timeout(1_500)
            peer = _telegram_peer_from_local_state(page, handle=handle)
    if peer is None:
        label = f"@{handle}" if handle else fragment
        raise CrawlerError(
            ErrorCode.BROWSE_FAILED,
            f"Telegram Web 无法解析频道 {label}。请先在该比特浏览器窗口中打开频道后重试。",
            retryable=True,
        )
    target_url = f"https://web.telegram.org/a/#{peer['peer_id']}"
    if page.url.rstrip("/") == target_url.rstrip("/") and page.locator(".MessageList").count():
        return page.url
    title = peer["title"]
    title_nodes = page.get_by_text(title, exact=True)
    if not title_nodes.count() and handle:
        search = page.locator('input[placeholder="Search"]').first
        if search.count():
            search.fill(handle)
            page.wait_for_timeout(1_500)
            title_nodes = page.get_by_text(title, exact=True)
    for index in range(title_nodes.count()):
        candidate = title_nodes.nth(index)
        if candidate.is_visible(timeout=300):
            candidate.click()
            page.wait_for_selector(".MessageList", state="attached", timeout=10_000)
            page.wait_for_timeout(500)
            return page.url
    raise CrawlerError(
        ErrorCode.BROWSE_FAILED,
        f"Telegram Web 已识别频道 {title}，但无法在当前窗口打开。请手动打开后重试。",
        retryable=True,
    )


def _telegram_peer_from_local_state(
    page: Page,
    *,
    handle: str | None = None,
    peer_id: str | None = None,
) -> dict[str, str] | None:
    result = page.evaluate(
        r"""
        async ({expected, expectedPeerId}) => {
          const databases = await indexedDB.databases();
          if (!databases.some((item) => item.name === 'tt-data')) return null;
          return await new Promise((resolve) => {
            const open = indexedDB.open('tt-data');
            open.onerror = () => resolve(null);
            open.onsuccess = () => {
              const request = open.result.transaction('store', 'readonly')
                .objectStore('store').get('tt-global-state');
              request.onerror = () => resolve(null);
              request.onsuccess = () => {
                const chats = request.result?.chats?.byId || {};
                for (const [peerId, chat] of Object.entries(chats)) {
                  const names = [chat?.username, ...(chat?.usernames || []).map((item) => item?.username)]
                    .filter(Boolean).map((item) => String(item).toLowerCase());
                  if ((expected && names.includes(expected)) ||
                      (expectedPeerId && String(peerId) === expectedPeerId)) {
                    resolve({peer_id: String(peerId), title: String(chat?.title || names[0] || peerId)});
                    return;
                  }
                }
                resolve(null);
              };
            };
          });
        }
        """,
        {"expected": handle, "expectedPeerId": peer_id},
    )
    return {"peer_id": str(result["peer_id"]), "title": str(result["title"])} if result else None


class TelegramWebDownloader:
    """Download message text and media inside an authenticated Telegram Web tab."""

    def __init__(self, progress_callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._progress_callback = progress_callback
        self.checkpoint_path: str | None = None
        self.completed = True
        self.stop_reason = "completed"
        self.scanned_count = 0

    def run(
        self,
        *,
        cdp_endpoint: str,
        request: DownloadInput,
        output_directory: Path,
    ) -> list[dict[str, Any]]:
        self.checkpoint_path = None
        self.completed = True
        self.stop_reason = "completed"
        self.scanned_count = 0
        if request.media_format is MediaFormat.AUDIO:
            raise CrawlerError(
                ErrorCode.INVALID_REQUEST,
                "Telegram Web 当前支持图文、视频和音视频下载，不支持仅音频模式。",
            )
        maximum_file_bytes = request.max_file_size_mb * 1024 * 1024
        maximum_total_bytes = request.max_total_size_mb * 1024 * 1024
        written_total = 0
        results: list[dict[str, Any]] = []
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.connect_over_cdp(
                    cdp_endpoint,
                    timeout=request.request_timeout_seconds * 1_000,
                )
                if not browser.contexts:
                    raise CrawlerError(ErrorCode.DOWNLOAD_FAILED, "比特浏览器没有可用的浏览上下文。")
                context = browser.contexts[0]
                pages = [
                    page
                    for page in context.pages
                    if not page.is_closed()
                    and (urlsplit(page.url).hostname or "").lower() == "web.telegram.org"
                ]
                page = pages[-1] if pages else context.new_page()
                page.set_default_timeout(request.request_timeout_seconds * 1_000)
                page.wait_for_timeout(500)
                if request.telegram_scope is TelegramDownloadScope.CHANNEL:
                    return self._run_channel(page, request, output_directory)
                active_channel: str | None = None
                for source in request.urls[: request.max_items]:
                    source_url = str(source)
                    channel_key, message_id, canonical_url, web_url = parse_telegram_post_url(source_url)
                    if active_channel != channel_key:
                        web_url = resolve_telegram_web_url(page, web_url)
                        if page.url.rstrip("/") != web_url.rstrip("/"):
                            page.goto(
                                web_url,
                                wait_until="domcontentloaded",
                                timeout=request.request_timeout_seconds * 1_000,
                            )
                        page.wait_for_timeout(900)
                        active_channel = channel_key
                    message = _find_message(page, message_id, request)
                    info, written_total, _ = self._download_message(
                        page=page,
                        message=message,
                        channel_key=channel_key,
                        message_id=message_id,
                        source_url=source_url,
                        canonical_url=canonical_url,
                        request=request,
                        output_directory=output_directory,
                        written_total=written_total,
                        maximum_file_bytes=maximum_file_bytes,
                        maximum_total_bytes=maximum_total_bytes,
                    )
                    results.append(info)
            except CrawlerError:
                raise
            except PlaywrightTimeoutError as exc:
                raise CrawlerError(
                    ErrorCode.PLATFORM_UNAVAILABLE,
                    "Telegram Web 页面或媒体读取超时，请检查网络和登录状态。",
                    retryable=True,
                ) from exc
            except Exception as exc:
                raise CrawlerError(
                    ErrorCode.DOWNLOAD_FAILED,
                    f"Telegram Web 下载失败：{type(exc).__name__}。",
                    retryable=False,
                ) from exc
        return results

    def _run_channel(
        self,
        page: Page,
        request: DownloadInput,
        output_directory: Path,
    ) -> list[dict[str, Any]]:
        channel_key, _, web_url = parse_telegram_channel_url(
            str(request.urls[0])
        )
        web_url = resolve_telegram_web_url(page, web_url)
        if page.url.rstrip("/") != web_url.rstrip("/"):
            page.goto(
                web_url,
                wait_until="domcontentloaded",
                timeout=request.request_timeout_seconds * 1_000,
            )
        page.wait_for_timeout(900)
        checkpoint = output_directory / "telegram-channel-manifest.jsonl"
        self.checkpoint_path = str(checkpoint)
        completed_rows = _read_channel_checkpoint(checkpoint)
        completed_ids = set(completed_rows)
        results = [completed_rows[key] for key in _sort_message_ids(completed_rows, reverse=True)]
        written_total = _directory_size(output_directory)
        maximum_file_bytes = request.max_file_size_mb * 1024 * 1024
        maximum_total_bytes = request.max_total_size_mb * 1024 * 1024
        stagnant_rounds = 0
        seen_visible: set[str] = set()

        while len(completed_ids) < request.telegram_max_messages:
            messages = page.locator(".Message.message-list-item[data-message-id]")
            visible: list[tuple[str, Any]] = []
            for index in range(messages.count()):
                message = messages.nth(index)
                try:
                    if not message.is_visible(timeout=200):
                        continue
                    message_id = str(message.get_attribute("data-message-id") or "")
                except PlaywrightTimeoutError:
                    continue
                if message_id.isdigit():
                    visible.append((message_id, message))
            visible.sort(key=lambda item: int(item[0]), reverse=True)
            new_visible = {message_id for message_id, _ in visible} - seen_visible
            seen_visible.update(message_id for message_id, _ in visible)
            stagnant_rounds = 0 if new_visible else stagnant_rounds + 1

            for message_id, message in visible:
                if message_id in completed_ids:
                    continue
                if len(completed_ids) >= request.telegram_max_messages:
                    self.completed = False
                    self.stop_reason = "message_limit"
                    break
                canonical_url = telegram_message_url(channel_key, message_id)
                try:
                    info, written_total, files = self._download_message(
                        page=page,
                        message=message,
                        channel_key=channel_key,
                        message_id=message_id,
                        source_url=canonical_url,
                        canonical_url=canonical_url,
                        request=request,
                        output_directory=output_directory,
                        written_total=written_total,
                        maximum_file_bytes=maximum_file_bytes,
                        maximum_total_bytes=maximum_total_bytes,
                    )
                except CrawlerError as exc:
                    if exc.code is not ErrorCode.LIMIT_EXCEEDED:
                        raise
                    self.completed = False
                    self.stop_reason = "size_limit"
                    break
                row = {**info, "files": files}
                _append_channel_checkpoint(checkpoint, row)
                completed_ids.add(message_id)
                completed_rows[message_id] = info
                results.append(info)
                self.scanned_count = len(completed_ids)
                if self._progress_callback is not None:
                    self._progress_callback(
                        {
                            "status": "channel_scanning",
                            "downloaded_messages": len(completed_ids),
                            "max_messages": request.telegram_max_messages,
                            "downloaded_bytes": written_total,
                            "total_bytes": maximum_total_bytes,
                        }
                    )
            if self.stop_reason in {"message_limit", "size_limit"}:
                break
            message_list = page.locator(".MessageList").first
            if not message_list.count():
                raise CrawlerError(ErrorCode.BROWSE_FAILED, "Telegram Web 没有可遍历的消息列表。")
            scroll_state = message_list.evaluate(
                "node => ({top: node.scrollTop, height: node.scrollHeight, client: node.clientHeight})"
            )
            if float(scroll_state.get("top") or 0) <= 1 and stagnant_rounds >= 1:
                self.stop_reason = "completed"
                break
            message_list.evaluate("node => node.scrollBy(0, -Math.max(node.clientHeight * 0.85, 900))")
            page.wait_for_timeout(700)
            if stagnant_rounds >= 4:
                self.completed = False
                self.stop_reason = "stagnant"
                break

        if len(completed_ids) >= request.telegram_max_messages:
            self.completed = False
            self.stop_reason = "message_limit"
        self.scanned_count = len(completed_ids)
        return sorted(results, key=lambda item: int(str(item["id"])), reverse=True)

    def _download_message(
        self,
        *,
        page: Page,
        message: Any,
        channel_key: str,
        message_id: str,
        source_url: str,
        canonical_url: str,
        request: DownloadInput,
        output_directory: Path,
        written_total: int,
        maximum_file_bytes: int,
        maximum_total_bytes: int,
    ) -> tuple[dict[str, Any], int, list[str]]:
        payload = _message_payload(message)
        text = str(payload.get("text") or "").strip()
        safe_stem = _safe_stem(f"Telegram-{channel_key}-{message_id}")
        files: list[str] = []
        if request.mode is DownloadMode.DOWNLOAD and text:
            text_bytes = text.encode("utf-8")
            written_total = _reserve_bytes(
                written_total,
                len(text_bytes),
                maximum_file_bytes,
                maximum_total_bytes,
            )
            text_path = output_directory / f"{safe_stem}.txt"
            text_path.write_bytes(text_bytes)
            files.append(str(text_path))

        media_sources: list[dict[str, str]] = list(payload.get("media") or [])
        if request.media_format is MediaFormat.VIDEO:
            media_sources = [item for item in media_sources if item.get("kind") == "video"]
        for index, media in enumerate(media_sources, start=1):
            if request.mode is not DownloadMode.DOWNLOAD:
                continue
            source_value = str(media.get("src") or "")
            kind = str(media.get("kind") or "")
            if not source_value or kind not in {"image", "video"}:
                continue
            target_base = output_directory / f"{safe_stem}-{index:02d}"
            path, size = _download_browser_media(
                page,
                source_value,
                kind,
                target_base,
                maximum_file_bytes=maximum_file_bytes,
                maximum_total_bytes=maximum_total_bytes - written_total,
                progress_callback=self._progress_callback,
            )
            written_total += size
            files.append(str(path))
            if self._progress_callback is not None:
                self._progress_callback(
                    {
                        "status": "finished",
                        "filename": str(path),
                        "downloaded_bytes": size,
                        "total_bytes": size,
                    }
                )
        return (
            {
                "id": message_id,
                "extractor_key": "TelegramWeb",
                "webpage_url": canonical_url,
                "title": f"Telegram {channel_key} #{message_id}",
                "description": text or None,
                "uploader": channel_key,
                "uploader_id": channel_key,
                "__source_url": source_url,
                "files_written": len(files),
            },
            written_total,
            files,
        )


def parse_telegram_post_url(value: str) -> tuple[str, str, str, str]:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        raise CrawlerError(ErrorCode.UNSUPPORTED_URL, "Telegram 下载地址必须是 t.me 帖子地址。")
    private_match = _PRIVATE_POST.match(parsed.path)
    if private_match:
        channel_id, message_id = private_match.groups()
        return (
            channel_id,
            message_id,
            f"https://t.me/c/{channel_id}/{message_id}",
            f"https://web.telegram.org/a/#-100{channel_id}",
        )
    public_match = _PUBLIC_POST.match(parsed.path)
    if not public_match:
        raise CrawlerError(
            ErrorCode.INVALID_REQUEST,
            "Telegram 下载需要具体消息地址，例如 https://t.me/channel/123。",
        )
    handle, message_id = public_match.groups()
    return (
        handle,
        message_id,
        f"https://t.me/{handle}/{message_id}",
        f"https://web.telegram.org/a/#@{handle}",
    )


def parse_telegram_channel_url(value: str) -> tuple[str, str, str]:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host == "web.telegram.org":
        fragment = parsed.fragment.strip()
        if fragment.startswith("@"):
            handle = fragment[1:].split("/", 1)[0]
            if re.fullmatch(r"[A-Za-z0-9_]{4,}", handle):
                return handle, f"https://t.me/{handle}", f"https://web.telegram.org/a/#@{handle}"
        if fragment.startswith("-100") and fragment[4:].split("_", 1)[0].isdigit():
            channel_id = fragment[4:].split("_", 1)[0]
            return channel_id, f"https://t.me/c/{channel_id}", f"https://web.telegram.org/a/#-100{channel_id}"
    if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        raise CrawlerError(ErrorCode.UNSUPPORTED_URL, "Telegram 频道下载地址必须来自 t.me。")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "c" and parts[1].isdigit():
        channel_id = parts[1]
        return channel_id, f"https://t.me/c/{channel_id}", f"https://web.telegram.org/a/#-100{channel_id}"
    if not parts or not re.fullmatch(r"[A-Za-z0-9_]{4,}", parts[0].lstrip("@")):
        raise CrawlerError(ErrorCode.INVALID_REQUEST, "Telegram 地址缺少有效的频道或群组标识。")
    handle = parts[0].lstrip("@")
    return handle, f"https://t.me/{handle}", f"https://web.telegram.org/a/#@{handle}"


def telegram_message_url(channel_key: str, message_id: str) -> str:
    if channel_key.isdigit():
        return f"https://t.me/c/{channel_key}/{message_id}"
    return f"https://t.me/{channel_key}/{message_id}"


def _read_channel_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
            message_id = str(payload.get("id") or "")
            if message_id.isdigit():
                payload.pop("files", None)
                rows[message_id] = payload
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return rows


def _append_channel_checkpoint(path: Path, row: dict[str, Any]) -> None:
    if path.is_file() and path.stat().st_size:
        with path.open("rb+") as raw:
            raw.seek(-1, 2)
            if raw.read(1) != b"\n":
                raw.seek(0, 2)
                raw.write(b"\n")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()


def _sort_message_ids(rows: dict[str, Any], *, reverse: bool) -> list[str]:
    return sorted(rows, key=int, reverse=reverse)


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _find_message(page: Page, message_id: str, request: DownloadInput):
    selector = f'.Message.message-list-item[data-message-id="{message_id}"]'
    message = page.locator(selector).first
    target_id = int(message_id)
    for _ in range(101):
        if message.count() and message.is_visible(timeout=300):
            return message
        message_list = page.locator(".MessageList").first
        if not message_list.count():
            break
        visible_ids = page.locator(
            ".Message.message-list-item[data-message-id]"
        ).evaluate_all(
            "nodes => nodes.map(node => Number(node.getAttribute('data-message-id'))).filter(Number.isFinite)"
        )
        if visible_ids and target_id > max(visible_ids):
            delta = 1_400
        else:
            delta = -1_400
        message_list.evaluate("(node, amount) => node.scrollBy(0, amount)", delta)
        page.wait_for_timeout(min(1_000, int(request.request_timeout_seconds * 100)))
    raise CrawlerError(
        ErrorCode.DOWNLOAD_FAILED,
        f"Telegram Web 当前频道中没有加载到消息 #{message_id}，请先在频道中定位该消息后重试。",
        retryable=True,
    )


def _message_payload(message: Any) -> dict[str, Any]:
    return message.evaluate(
        r"""
        (node) => {
          const text = node.querySelector('.text-content')?.textContent?.trim() ||
            node.querySelector('.message-content')?.textContent?.trim() || '';
          const media = [];
          const seen = new Set();
          for (const container of node.querySelectorAll('.media-inner')) {
            const video = container.querySelector('video[src]');
            const image = container.querySelector('img.full-media[src], img[src^="blob:"]');
            const element = video || image;
            const src = element?.currentSrc || element?.src || '';
            if (!src || seen.has(src)) continue;
            seen.add(src);
            media.push({kind: video ? 'video' : 'image', src});
          }
          return {text, media};
        }
        """
    )


def _download_browser_media(
    page: Page,
    source_url: str,
    kind: str,
    target_base: Path,
    *,
    maximum_file_bytes: int,
    maximum_total_bytes: int,
    progress_callback: Callable[[dict[str, Any]], None] | None,
) -> tuple[Path, int]:
    first = _fetch_browser_chunk(page, source_url, 0 if kind == "video" else None)
    mime = str(first.get("mime") or "").split(";", 1)[0].lower()
    total = _response_total(first)
    if total is None:
        total = len(base64.b64decode(str(first.get("data") or "")))
    if total > maximum_file_bytes or total > maximum_total_bytes:
        raise CrawlerError(
            ErrorCode.LIMIT_EXCEEDED,
            f"Telegram 媒体文件大小 {total / 1024 / 1024:.1f} MB 超过当前下载上限。",
        )
    extension = _media_extension(kind, mime)
    target = target_base.with_suffix(extension)
    partial = target.with_suffix(f"{extension}.part")
    written = 0
    try:
        with partial.open("wb") as stream:
            response = first
            while True:
                chunk = base64.b64decode(str(response.get("data") or ""))
                if not chunk:
                    break
                stream.write(chunk)
                written += len(chunk)
                if progress_callback is not None:
                    progress_callback(
                        {
                            "status": "downloading",
                            "filename": str(target),
                            "downloaded_bytes": written,
                            "total_bytes": total,
                        }
                    )
                if written >= total or kind != "video":
                    break
                response = _fetch_browser_chunk(page, source_url, written)
        if written != total:
            raise CrawlerError(
                ErrorCode.DOWNLOAD_FAILED,
                f"Telegram 媒体下载不完整：预期 {total} 字节，实际 {written} 字节。",
                retryable=True,
            )
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target, written


def _fetch_browser_chunk(page: Page, source_url: str, start: int | None) -> dict[str, Any]:
    return page.evaluate(
        r"""
        async ({url, start, chunkSize}) => {
          const headers = start === null ? {} : {Range: `bytes=${start}-${start + chunkSize - 1}`};
          const response = await fetch(url, {headers, credentials: 'include'});
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const bytes = new Uint8Array(await response.arrayBuffer());
          let binary = '';
          const stride = 0x8000;
          for (let index = 0; index < bytes.length; index += stride) {
            binary += String.fromCharCode(...bytes.subarray(index, index + stride));
          }
          return {
            data: btoa(binary),
            mime: response.headers.get('content-type') || '',
            contentRange: response.headers.get('content-range') || '',
            contentLength: response.headers.get('content-length') || '',
          };
        }
        """,
        {"url": source_url, "start": start, "chunkSize": _FETCH_CHUNK_BYTES},
    )


def _response_total(response: dict[str, Any]) -> int | None:
    match = _CONTENT_RANGE.fullmatch(str(response.get("contentRange") or "").strip())
    if match and match.group(3) != "*":
        return int(match.group(3))
    try:
        value = int(str(response.get("contentLength") or ""))
        return value if value >= 0 else None
    except ValueError:
        return None


def _media_extension(kind: str, mime: str) -> str:
    if kind == "video":
        return ".webm" if mime == "video/webm" else ".mp4"
    return {
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime, ".jpg")


def _reserve_bytes(current: int, added: int, maximum_file: int, maximum_total: int) -> int:
    if added > maximum_file or current + added > maximum_total:
        raise CrawlerError(ErrorCode.LIMIT_EXCEEDED, "Telegram 下载内容超过当前大小上限。")
    return current + added


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned[:160] or "Telegram-message"
