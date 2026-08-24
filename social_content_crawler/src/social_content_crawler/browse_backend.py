from __future__ import annotations

import re
import threading
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Callable, Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .browse_contracts import (
    BrowsePlatform,
    BrowsePostsInput,
    BrowsePostsOutput,
    BrowsedPost,
    BrowseSource,
    BrowseView,
    PostMetrics,
)
from .errors import CrawlerError, ErrorCode
from .sessions import BitBrowserClient, SessionRegistry


_X_POST_PATH = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/(\d+)")
_DOUYIN_POST_PATH = re.compile(r"^/(?:video|note)/(\d+)")
_XHS_POST_PATH = re.compile(r"^/(?:explore|discovery/item)/([A-Za-z0-9]+)")
_NUMBER = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*([KMB万亿]?)", re.IGNORECASE)
_PLATFORM_LABEL = {
    BrowsePlatform.X: "X / Twitter",
    BrowsePlatform.DOUYIN: "抖音",
    BrowsePlatform.XIAOHONGSHU: "小红书",
}


class BrowserAutomation(Protocol):
    def collect(
        self,
        *,
        cdp_endpoint: str,
        source_url: str,
        request: BrowsePostsInput,
    ) -> tuple[list[dict[str, Any]], bool, list[str]]: ...


class PlaywrightCdpAutomation:
    """Platform-specific DOM collectors connected to an authorized BitBrowser profile."""

    def collect(
        self,
        *,
        cdp_endpoint: str,
        source_url: str,
        request: BrowsePostsInput,
    ) -> tuple[list[dict[str, Any]], bool, list[str]]:
        rows_by_url: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        label = _PLATFORM_LABEL[request.platform]
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.connect_over_cdp(
                    cdp_endpoint,
                    timeout=request.navigation_timeout_seconds * 1_000,
                )
                if not browser.contexts:
                    raise CrawlerError(ErrorCode.BROWSE_FAILED, "比特浏览器没有可用的浏览上下文。")
                page = browser.contexts[0].new_page()
                page.set_default_timeout(request.navigation_timeout_seconds * 1_000)
                try:
                    page.goto(
                        source_url,
                        wait_until="domcontentloaded",
                        timeout=request.navigation_timeout_seconds * 1_000,
                    )
                    _raise_if_login_required(page, request.platform)
                    stagnant_rounds = 0
                    for scroll_index in range(request.max_scrolls + 1):
                        before = len(rows_by_url)
                        for row in _extract_rows(page, request.platform):
                            url = str(row.get("url") or "")
                            if url:
                                rows_by_url.setdefault(url, row)
                            if len(rows_by_url) >= request.max_items:
                                break
                        if len(rows_by_url) >= request.max_items:
                            return list(rows_by_url.values())[: request.max_items], True, warnings
                        if scroll_index >= request.max_scrolls:
                            break
                        stagnant_rounds = stagnant_rounds + 1 if len(rows_by_url) == before else 0
                        if stagnant_rounds >= 3:
                            break
                        page.mouse.wheel(0, 1_200)
                        page.wait_for_timeout(request.settle_after_scroll_ms)
                        _raise_if_login_required(page, request.platform)
                finally:
                    page.close()
            except CrawlerError:
                raise
            except PlaywrightTimeoutError as exc:
                raise CrawlerError(
                    ErrorCode.PLATFORM_UNAVAILABLE,
                    f"{label} 页面加载超时。请检查该 Profile 的网络连接和登录状态。",
                    retryable=True,
                ) from exc
            except Exception as exc:
                raise CrawlerError(
                    ErrorCode.BROWSE_FAILED,
                    f"无法通过比特浏览器读取 {label} 页面。",
                    retryable=False,
                ) from exc
        if not rows_by_url:
            warnings.append(
                f"{label} 页面中没有发现可识别的帖子；页面可能为空、加载失败或结构已经变化。"
            )
        return list(rows_by_url.values())[: request.max_items], False, warnings


class SocialPostBrowserBackend:
    def __init__(
        self,
        *,
        session_registry: SessionRegistry,
        automation: BrowserAutomation | None = None,
        client_factory: Callable[[str], BitBrowserClient] = BitBrowserClient,
    ) -> None:
        self._session_registry = session_registry
        self._automation = automation or PlaywrightCdpAutomation()
        self._client_factory = client_factory
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

    def run(self, request: BrowsePostsInput) -> BrowsePostsOutput:
        lock = self._locks[request.session_ref]
        if not lock.acquire(timeout=5.0):
            raise CrawlerError(
                ErrorCode.SESSION_BUSY,
                "该 session_ref 正在执行另一个浏览任务，请稍后重试。",
                retryable=True,
            )
        try:
            record = self._session_registry.validate_session(
                request.session_ref,
                request.platform.value,
            )
            client = self._client_factory(record.api_url)
            cdp_endpoint = client.open_profile(record.profile_id)
            source_url = build_source_url(request)
            rows, truncated, warnings = self._automation.collect(
                cdp_endpoint=cdp_endpoint,
                source_url=source_url,
                request=request,
            )
            posts = normalize_rows(request.platform, rows, request.max_items)
            return BrowsePostsOutput(
                platform=request.platform,
                source_url=source_url,
                posts=posts,
                truncated=truncated,
                warnings=warnings,
                observed_at=datetime.now(UTC),
            )
        finally:
            lock.release()


XPostBrowserBackend = SocialPostBrowserBackend


def build_source_url(request: BrowsePostsInput) -> str:
    if request.source is BrowseSource.URL:
        assert request.start_url is not None
        return str(request.start_url)
    if request.platform is BrowsePlatform.X:
        return _build_x_source_url(request)
    if request.platform is BrowsePlatform.DOUYIN:
        return _build_douyin_source_url(request)
    return _build_xhs_source_url(request)


def build_x_source_url(request: BrowsePostsInput) -> str:
    if request.platform is not BrowsePlatform.X:
        raise CrawlerError(ErrorCode.INVALID_REQUEST, "build_x_source_url 只接受 X 请求。")
    return build_source_url(request)


def _build_x_source_url(request: BrowsePostsInput) -> str:
    if request.source is BrowseSource.SEARCH:
        filter_value = {
            BrowseView.TOP: None,
            BrowseView.LATEST: "live",
            BrowseView.MEDIA: "media",
        }[request.view]
        params = {"q": (request.query or "").strip(), "src": "typed_query"}
        if filter_value:
            params["f"] = filter_value
        return f"https://x.com/search?{urlencode(params)}"
    if request.source is BrowseSource.USER:
        base = f"https://x.com/{quote(request.user_key or '', safe='')}"
        suffix = {
            BrowseView.POSTS: "",
            BrowseView.MEDIA: "/media",
            BrowseView.REPLIES: "/with_replies",
        }[request.view]
        return f"{base}{suffix}"
    return "https://x.com/home"


def _build_douyin_source_url(request: BrowsePostsInput) -> str:
    if request.source is BrowseSource.SEARCH:
        search_type = {
            BrowseView.TOP: "general",
            BrowseView.MEDIA: "video",
            BrowseView.USERS: "user",
        }[request.view]
        query = quote((request.query or "").strip(), safe="")
        return f"https://www.douyin.com/search/{query}?type={search_type}"
    if request.source is BrowseSource.USER:
        return f"https://www.douyin.com/user/{quote(request.user_key or '', safe='')}"
    return "https://www.douyin.com/"


def _build_xhs_source_url(request: BrowsePostsInput) -> str:
    if request.source is BrowseSource.SEARCH:
        params = {
            "keyword": (request.query or "").strip(),
            "source": "web_search_result_notes",
        }
        if request.view is BrowseView.LATEST:
            params["sort"] = "time_descending"
        elif request.view is BrowseView.MEDIA:
            params["note_type"] = "video"
        return f"https://www.xiaohongshu.com/search_result?{urlencode(params)}"
    if request.source is BrowseSource.USER:
        key = quote(request.user_key or "", safe="")
        return f"https://www.xiaohongshu.com/user/profile/{key}"
    return "https://www.xiaohongshu.com/explore"


def normalize_rows(
    platform: BrowsePlatform,
    rows: list[dict[str, Any]],
    max_items: int,
) -> list[BrowsedPost]:
    posts: list[BrowsedPost] = []
    seen: set[str] = set()
    for row in rows:
        identity = _post_identity(platform, str(row.get("url") or ""))
        if identity is None:
            continue
        canonical_url, post_id, author_id, author_handle = identity
        identity_key = f"{platform.value}:{post_id}"
        if identity_key in seen:
            continue
        seen.add(identity_key)
        media_types = []
        if row.get("has_image"):
            media_types.append("image")
        if row.get("has_video"):
            media_types.append("video")
        posts.append(
            BrowsedPost(
                url=canonical_url,
                post_id=post_id,
                author_id=_clean_text(row.get("author_id"), 300) or author_id,
                author_handle=author_handle,
                author_name=_clean_text(row.get("author_name"), 300),
                text=_clean_text(row.get("text"), 10_000),
                language=_clean_text(row.get("language"), 32),
                published_at=row.get("published_at") or None,
                media_types=media_types,
                metrics=PostMetrics(
                    replies=_metric_number(row.get("replies")),
                    reposts=_metric_number(row.get("reposts")),
                    likes=_metric_number(row.get("likes")),
                    views=_metric_number(row.get("views")),
                ),
                position=len(posts) + 1,
            )
        )
        if len(posts) >= max_items:
            break
    return posts


def _post_identity(
    platform: BrowsePlatform,
    value: str,
) -> tuple[str, str, str | None, str | None] | None:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if platform is BrowsePlatform.X:
        match = _X_POST_PATH.match(parsed.path)
        if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"} or not match:
            return None
        handle, post_id = match.groups()
        return f"https://x.com/{handle}/status/{post_id}", post_id, handle, handle
    if platform is BrowsePlatform.DOUYIN:
        match = _DOUYIN_POST_PATH.match(parsed.path)
        if not (host == "douyin.com" or host.endswith(".douyin.com")) or not match:
            return None
        post_id = match.group(1)
        return f"https://www.douyin.com/video/{post_id}", post_id, None, None
    match = _XHS_POST_PATH.match(parsed.path)
    if not (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")) or not match:
        return None
    post_id = match.group(1)
    canonical = f"https://www.xiaohongshu.com/explore/{post_id}"
    access_params = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key in {"xsec_token", "xsec_source"}
    ]
    if access_params:
        canonical = f"{canonical}?{urlencode(access_params)}"
    return canonical, post_id, None, None


def _extract_rows(page: Page, platform: BrowsePlatform) -> list[dict[str, Any]]:
    if platform is BrowsePlatform.X:
        return _extract_x_rows(page)
    if platform is BrowsePlatform.DOUYIN:
        return _extract_douyin_rows(page)
    return _extract_xhs_rows(page)


def _extract_x_rows(page: Page) -> list[dict[str, Any]]:
    return page.locator('article[data-testid="tweet"]').evaluate_all(
        r"""
        (articles) => articles.map((article) => {
          const statusLinks = [...article.querySelectorAll('a[href*="/status/"]')];
          const statusLink = statusLinks.find((link) => /^\/[A-Za-z0-9_]{1,15}\/status\/\d+/.test(link.getAttribute('href') || ''));
          const userName = article.querySelector('[data-testid="User-Name"]');
          const nameSpans = userName ? [...userName.querySelectorAll('span')] : [];
          const authorName = nameSpans.map((span) => span.textContent?.trim()).find((value) => value && !value.startsWith('@')) || null;
          const textNode = article.querySelector('[data-testid="tweetText"]');
          const metric = (testId) => {
            const node = article.querySelector(`[data-testid="${testId}"]`);
            return node?.getAttribute('aria-label') || node?.textContent?.trim() || null;
          };
          const analytics = [...article.querySelectorAll('a[href$="/analytics"]')][0];
          return {
            url: statusLink ? new URL(statusLink.getAttribute('href'), 'https://x.com').href : null,
            author_name: authorName,
            text: textNode?.textContent?.trim() || null,
            language: textNode?.getAttribute('lang') || null,
            published_at: article.querySelector('time[datetime]')?.getAttribute('datetime') || null,
            replies: metric('reply'), reposts: metric('retweet'), likes: metric('like'),
            views: analytics?.getAttribute('aria-label') || analytics?.textContent?.trim() || null,
            has_image: Boolean(article.querySelector('[data-testid="tweetPhoto"] img')),
            has_video: Boolean(article.querySelector('video, [data-testid="videoPlayer"]')),
          };
        }).filter((row) => row.url)
        """
    )


def _extract_douyin_rows(page: Page) -> list[dict[str, Any]]:
    return page.locator('a[href*="/video/"], a[href*="/note/"]').evaluate_all(
        r"""
        (links) => links.map((link) => {
          const href = link.getAttribute('href') || '';
          if (!/^\/(video|note)\/\d+/.test(new URL(href, location.origin).pathname)) return null;
          const card = link.closest('li, article, [data-e2e*="feed"], [data-e2e*="search"]') || link.parentElement;
          const authorLink = card?.querySelector('a[href*="/user/"]');
          const titleNode = card?.querySelector('[data-e2e*="desc"], [class*="title"], [class*="desc"], h1, h2, h3');
          return {
            url: new URL(href, 'https://www.douyin.com').href,
            author_id: authorLink ? new URL(authorLink.getAttribute('href'), location.origin).pathname.split('/user/')[1]?.split('/')[0] : null,
            author_name: authorLink?.textContent?.trim() || null,
            text: link.getAttribute('title') || titleNode?.textContent?.trim() || link.getAttribute('aria-label') || null,
            published_at: card?.querySelector('time[datetime]')?.getAttribute('datetime') || null,
            likes: card?.querySelector('[data-e2e*="like"]')?.textContent?.trim() || null,
            has_image: Boolean(card?.querySelector('img')),
            has_video: true,
          };
        }).filter(Boolean)
        """
    )


def _extract_xhs_rows(page: Page) -> list[dict[str, Any]]:
    return page.locator('a[href*="/explore/"], a[href*="/discovery/item/"]').evaluate_all(
        r"""
        (links) => links.map((link) => {
          const href = link.getAttribute('href') || '';
          const path = new URL(href, location.origin).pathname;
          if (!/^\/(explore|discovery\/item)\/[A-Za-z0-9]+/.test(path)) return null;
          const card = link.closest('section, article, li, [class*="note-item"]') || link.parentElement;
          const authorLink = card?.querySelector('a[href*="/user/profile/"]');
          const titleNode = card?.querySelector('[class*="title"], .title, h1, h2, h3');
          return {
            url: new URL(href, 'https://www.xiaohongshu.com').href,
            author_id: authorLink ? new URL(authorLink.getAttribute('href'), location.origin).pathname.split('/user/profile/')[1]?.split('/')[0] : null,
            author_name: authorLink?.textContent?.trim() || null,
            text: link.getAttribute('title') || titleNode?.textContent?.trim() || link.getAttribute('aria-label') || null,
            published_at: card?.querySelector('time[datetime]')?.getAttribute('datetime') || null,
            likes: card?.querySelector('[class*="like"]')?.textContent?.trim() || null,
            has_image: Boolean(card?.querySelector('img')),
            has_video: Boolean(card?.querySelector('video, [class*="video"]')),
          };
        }).filter(Boolean)
        """
    )


def _raise_if_login_required(page: Page, platform: BrowsePlatform) -> None:
    path = urlsplit(page.url).path.lower()
    login_paths = {
        BrowsePlatform.X: ("/i/flow/login", "/login"),
        BrowsePlatform.DOUYIN: ("/passport/login",),
        BrowsePlatform.XIAOHONGSHU: ("/login",),
    }[platform]
    if any(path.startswith(prefix) for prefix in login_paths):
        raise CrawlerError(
            ErrorCode.SESSION_REAUTH_REQUIRED,
            f"{_PLATFORM_LABEL[platform]} 登录会话已失效，请在对应比特浏览器 Profile 中重新登录。",
        )


def _metric_number(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if not isinstance(value, str):
        return None
    match = _NUMBER.search(value.replace(",", ""))
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    multiplier = {
        "": 1,
        "K": 1_000,
        "M": 1_000_000,
        "B": 1_000_000_000,
        "万": 10_000,
        "亿": 100_000_000,
    }.get(match.group(2).upper(), 1)
    return int(number * multiplier)


def _clean_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned[:limit] if cleaned else None
