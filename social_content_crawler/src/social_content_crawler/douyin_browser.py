"""Read one exact Douyin post from its authorized browser, without API signing.

The page makes its own authenticated requests. We inspect only response/page
data for the requested aweme, never pick a random playing recommendation.
Media transfer remains in the downloader using the same profile proxy/cookies.
"""
from __future__ import annotations

import json
import re
from time import monotonic
from urllib.parse import parse_qs, unquote, urlsplit

from playwright.sync_api import sync_playwright

from .browse_backend import (
    _existing_platform_page, _raise_if_login_required,
    _raise_if_platform_challenge, _wait_for_restored_tabs,
)
from .browse_contracts import BrowsePlatform
from .errors import CrawlerError, ErrorCode
from .diagnostics import record_exception


def post_id_from_url(url: str) -> str | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host != "douyin.com" and not host.endswith(".douyin.com"):
        return None
    modal = parse_qs(parsed.query).get("modal_id", [""])[0]
    if modal.isdigit():
        return modal
    match = re.fullmatch(r"/(?:video|note)/(\d+)/?", parsed.path)
    return match.group(1) if match else None


def _media_urls(value) -> list[str]:
    """Only accept platform CDN addresses, not arbitrary URLs from page data."""
    if isinstance(value, dict):
        value = value.get("url_list") or value.get("urlList") or value.get("src") or []
    if isinstance(value, str):
        value = [value]
    result = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            result.extend(_media_urls(item))
            continue
        if not isinstance(item, str):
            continue
        if item.startswith("//"):
            item = "https:" + item
        parsed = urlsplit(item)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme == "https" and not parsed.username and not parsed.password
                and any(host == d or host.endswith("." + d) for d in
                        ("douyinvod.com", "douyin.com", "byteimg.com", "douyinpic.com", "ibyteimg.com"))):
            result.append(item)
    return list(dict.fromkeys(result))


def _number(value, scale=1):
    try:
        return float(value) / scale if value is not None else None
    except (ValueError, TypeError):
        return None


def media_info_from_payload(payload, post_id: str, page_url: str) -> dict | None:
    """Bounded traversal of detail/list/SSR variants; require an exact post ID."""
    queue = [payload]
    visited = 0
    while queue and visited < 50_000:
        node = queue.pop()
        visited += 1
        if isinstance(node, list):
            queue.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        identity = node.get("aweme_id", node.get("awemeId"))
        if str(identity) == post_id:
            info = _post_media(node, post_id, page_url)
            if info:
                return info
        queue.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return None


def _post_media(node: dict, post_id: str, page_url: str) -> dict | None:
    video = node.get("video") or {}
    images = node.get("images") or (node.get("image_post_info") or {}).get("images") or []
    thumbnails = []
    for index, image in enumerate(images):
        urls = _media_urls(image.get("display_image", image)) if isinstance(image, dict) else []
        if urls:
            thumbnails.append({"id": str(index + 1), "url": urls[0]})
    # Never treat the music/placeholder video attached to an image post as its media.
    if images and len(thumbnails) != len(images):
        return None
    formats = []
    if not images and isinstance(video, dict):
        addresses = [(video.get(k), video) for k in ("play_addr", "playAddr", "play_addr_h264", "play_addr_265")]
        for variant in video.get("bit_rate", video.get("bitRate", [])) or []:
            if isinstance(variant, dict):
                addresses.append((variant.get("play_addr", variant.get("playAddr")), variant))
        seen = set()
        for address, metadata in addresses:
            for url in _media_urls(address):
                if url not in seen:
                    seen.add(url)
                    addr_meta = address if isinstance(address, dict) else {}
                    formats.append({
                        "format_id": str(len(formats)), "url": url, "ext": "mp4",
                        "width": _number(addr_meta.get("width") or metadata.get("width")),
                        "height": _number(addr_meta.get("height") or metadata.get("height")),
                        "tbr": _number(metadata.get("bit_rate"), 1000),
                        "filesize": _number(addr_meta.get("data_size")),
                        "vcodec": "h265" if metadata.get("is_h265") or metadata.get("is_bytevc1") else "h264",
                        "acodec": "aac",
                        # API redirect URLs may reject external requests even
                        # when the equivalent web CDN rendition works.
                        "source_preference": 0 if "douyinvod.com" in urlsplit(url).hostname else -2,
                    })
    if not formats and not thumbnails:
        return None
    author = node.get("author") or {}
    stats = node.get("statistics") or {}
    description = str(node.get("desc") or node.get("description") or "")
    return {
        "id": post_id, "extractor_key": "DouyinBrowser", "extractor": "douyin:browser",
        "webpage_url": page_url, "__source_url": page_url,
        "title": description[:200] or f"Douyin-{post_id}", "description": description,
        "uploader": author.get("nickname"), "uploader_id": str(author.get("uid") or ""),
        "timestamp": _number(node.get("create_time", node.get("createTime"))),
        "duration": _number(video.get("duration"), 1000) if formats else None,
        "like_count": _number(stats.get("digg_count")), "comment_count": _number(stats.get("comment_count")),
        "formats": formats, "thumbnails": thumbnails,
    }


def extract_from_browser(*, cdp_endpoint: str, page_url: str, timeout: float) -> dict:
    target = post_id_from_url(page_url)
    if target is None:
        raise CrawlerError(ErrorCode.INVALID_REQUEST, "抖音浏览器下载需要具体的 video/note 帖子地址。")
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_endpoint, timeout=timeout * 1000)
        if not browser.contexts:
            raise CrawlerError(ErrorCode.DOWNLOAD_FAILED, "比特浏览器没有可用的浏览上下文。")
        context = browser.contexts[0]
        _wait_for_restored_tabs(context)
        page = _existing_platform_page(context.pages, BrowsePlatform.DOUYIN)
        created = page is None
        from .browser_lifecycle import new_task_page, task_manages_pages
        page = page if page is not None else new_task_page(context, cdp_endpoint)
        try:
            info = _read_post(page, page_url, target, timeout)
            info["http_headers"] = {"Referer": page_url, "User-Agent": page.evaluate("navigator.userAgent")}
            for thumbnail in info.get("thumbnails", []):
                thumbnail["http_headers"] = dict(info["http_headers"])
            return info
        finally:
            if created and not page.is_closed():
                if not task_manages_pages():
                    page.close()
            # Exiting Playwright disconnects CDP. Never close the user's browser.


def _read_post(page, page_url: str, target: str, timeout: float) -> dict:
    found = []
    parse_error_logged = False

    def on_response(response):
        nonlocal parse_error_logged
        parsed = urlsplit(response.url)
        host = (parsed.hostname or "").lower()
        if (host != "douyin.com" and not host.endswith(".douyin.com")) or "/aweme/" not in parsed.path:
            return
        if "json" not in response.headers.get("content-type", ""):
            return
        try:
            info = media_info_from_payload(response.json(), target, page_url)
            if info:
                found.append(info)
        except (ValueError, TypeError):
            pass  # Not a detail payload. Never log raw responses or signed URLs.
        except Exception as exc:
            if not parse_error_logged:
                record_exception("social-content", "download.douyin.response", exc)
                parse_error_logged = True

    page.on("response", on_response)
    try:
        page.goto(page_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            _raise_if_login_required(page, BrowsePlatform.DOUYIN)
            _raise_if_platform_challenge(page, BrowsePlatform.DOUYIN)
            if post_id_from_url(page.url) != target:
                raise CrawlerError(ErrorCode.DOWNLOAD_FAILED, "抖音页面未停留在目标帖子，未下载其他推荐内容。")
            if found:
                return found[0]
            # Only serialized public page state; do not inspect cookies/storage.
            for raw in page.locator('script#RENDER_DATA, script#__NEXT_DATA__, script[type="application/json"]').all_text_contents():
                if not raw or len(raw) > 10_000_000:
                    continue
                try:
                    info = media_info_from_payload(json.loads(unquote(raw)), target, page_url)
                    if info:
                        return info
                except (ValueError, TypeError):
                    continue
            # The current web app also hydrates its public loader data here.
            # Read only post fields from exact-ID matches, not account state.
            payload = page.evaluate(r"""target => {
              const queue = [window._ROUTER_DATA, window.__INITIAL_STATE__, window.SIGI_STATE];
              const seen = new Set(), result = [];
              while(queue.length && seen.size < 50000) {
                const v = queue.pop();
                if(!v || typeof v !== 'object' || seen.has(v)) continue;
                seen.add(v);
                if(String(v.aweme_id ?? v.awemeId) === target) {
                  result.push({aweme_id:target, desc:v.desc, description:v.description,
                    video:v.video, images:v.images, image_post_info:v.image_post_info,
                    author:{nickname:v.author?.nickname,uid:v.author?.uid},
                    create_time:v.create_time ?? v.createTime});
                }
                for(const item of Object.values(v)) if(item && typeof item === 'object') queue.push(item);
              }
              return result;
            }""", target)
            info = media_info_from_payload(payload, target, page_url)
            if info:
                return info
            page.wait_for_timeout(250)
        raise CrawlerError(ErrorCode.DOWNLOAD_FAILED, "比特浏览器未返回目标抖音帖子的媒体数据；请检查页面加载、登录或验证状态。")
    finally:
        page.remove_listener("response", on_response)
