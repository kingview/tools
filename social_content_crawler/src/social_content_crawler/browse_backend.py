from __future__ import annotations

import re
from time import monotonic, sleep
from uuid import uuid4
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
from .profile_tasks import GLOBAL_PROFILE_TASK_COORDINATOR, ProfileTaskCoordinator
from .sessions import BitBrowserClient, SessionRegistry
from .telegram_web import resolve_telegram_web_url
from .telegram_dom import MESSAGE_LIST, MESSAGES, message_list, seek_latest


_X_POST_PATH = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/(\d+)")
_DOUYIN_POST_PATH = re.compile(r"^/(?:video|note)/(\d+)")
_XHS_POST_PATH = re.compile(
    r"^/(?:explore|discovery/item|search_result)/([A-Za-z0-9]+)"
)
_TELEGRAM_PUBLIC_POST_PATH = re.compile(r"^/([A-Za-z0-9_]{4,})/(\d+)")
_TELEGRAM_PRIVATE_POST_PATH = re.compile(r"^/c/(\d+)/(\d+)")
_NUMBER = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*([KMB万亿]?)", re.IGNORECASE)
_PLATFORM_LABEL = {
    BrowsePlatform.X: "X / Twitter",
    BrowsePlatform.DOUYIN: "抖音",
    BrowsePlatform.XIAOHONGSHU: "小红书",
    BrowsePlatform.TELEGRAM: "Telegram Web",
}
_POST_SELECTORS = {
    BrowsePlatform.X: 'article[data-testid="tweet"]',
    BrowsePlatform.DOUYIN: (
        'a[href*="/video/"], a[href*="/note/"], '
        '[data-aweme-id], [id^="waterfall_item_"]'
    ),
    BrowsePlatform.XIAOHONGSHU: (
        'a[href*="/explore/"], a[href*="/discovery/item/"]'
    ),
    BrowsePlatform.TELEGRAM: f"{MESSAGE_LIST} {MESSAGES}",
}
_CHALLENGE_SELECTORS = (
    'iframe[src*="captcha" i], iframe[src*="verify" i], '
    '[id*="captcha" i], [class*="captcha" i], '
    '[data-e2e*="captcha" i], [data-testid*="captcha" i], '
    '.secsdk-captcha-drag-icon, .captcha_verify_container'
)
_CHALLENGE_TEXT_MARKERS = (
    "请完成下列验证",
    "请完成安全验证",
    "请完成图片验证",
    "点击图中",
    "请选择所有",
    "拖动滑块",
    "验证后继续",
    "图片验证",
    "安全验证",
)


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
                context = browser.contexts[0]
                _wait_for_restored_tabs(context)
                page = _existing_platform_page(context.pages, request.platform)
                created_page = page is None
                if page is None:
                    from .browser_lifecycle import new_task_page
                    page = new_task_page(context, cdp_endpoint)
                page.set_default_timeout(request.navigation_timeout_seconds * 1_000)
                try:
                    if request.platform is BrowsePlatform.TELEGRAM:
                        source_url = resolve_telegram_web_url(page, source_url)
                    if not _same_navigation_url(page.url, source_url):
                        page.goto(
                            source_url,
                            wait_until="domcontentloaded",
                            timeout=request.navigation_timeout_seconds * 1_000,
                        )
                    _raise_if_login_required(page, request.platform)
                    page.wait_for_timeout(request.settle_after_scroll_ms)
                    challenge_wait_ms = int(
                        max(
                            60_000,
                            min(request.navigation_timeout_seconds * 1_000, 120_000),
                        )
                    )
                    _raise_if_platform_challenge(
                        page,
                        request.platform,
                        wait_timeout_ms=challenge_wait_ms,
                    )
                    _wait_for_initial_posts(page, request)
                    if request.platform is BrowsePlatform.TELEGRAM:
                        seek_latest(page, timeout_seconds=request.navigation_timeout_seconds)
                    _raise_if_platform_challenge(
                        page,
                        request.platform,
                        wait_timeout_ms=challenge_wait_ms,
                    )
                    stagnant_rounds = 0
                    for scroll_index in range(request.max_scrolls + 1):
                        before = len(rows_by_url)
                        for row in _extract_rows(page, request):
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
                        if request.platform is BrowsePlatform.TELEGRAM:
                            message_list(page).evaluate(
                                "node => node.scrollBy(0, -1200)"
                            )
                        else:
                            page.mouse.wheel(0, 1_200)
                        page.wait_for_timeout(request.settle_after_scroll_ms)
                        _raise_if_login_required(page, request.platform)
                        _raise_if_platform_challenge(
                            page,
                            request.platform,
                            wait_timeout_ms=challenge_wait_ms,
                        )
                finally:
                    from .browser_lifecycle import task_manages_pages
                    if created_page and not task_manages_pages() and not page.is_closed():
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
        task_coordinator: ProfileTaskCoordinator = GLOBAL_PROFILE_TASK_COORDINATOR,
    ) -> None:
        self._session_registry = session_registry
        self._automation = automation or PlaywrightCdpAutomation()
        self._client_factory = client_factory
        self._task_coordinator = task_coordinator

    def run(self, request: BrowsePostsInput) -> BrowsePostsOutput:
        record = self._session_registry.validate_session(
            request.session_ref,
            request.platform.value,
        )
        with self._task_coordinator.hold(record.api_url, record.profile_id):
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
                next_cursor=(posts[-1].post_id if truncated and posts else None),
                warnings=warnings,
                observed_at=datetime.now(UTC),
            )


XPostBrowserBackend = SocialPostBrowserBackend


def build_source_url(request: BrowsePostsInput) -> str:
    if request.source is BrowseSource.URL:
        assert request.start_url is not None
        if request.platform is BrowsePlatform.TELEGRAM:
            return _normalize_telegram_source_url(str(request.start_url))
        return str(request.start_url)
    if request.platform is BrowsePlatform.X:
        return _build_x_source_url(request)
    if request.platform is BrowsePlatform.DOUYIN:
        return _build_douyin_source_url(request)
    if request.platform is BrowsePlatform.XIAOHONGSHU:
        return _build_xhs_source_url(request)
    return _build_telegram_source_url(request)


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
        # The current desktop search page only hydrates results when the request
        # includes the client navigation id that Douyin adds after submitting the
        # search box.  Without it the same URL renders only the navigation shell.
        return f"https://www.douyin.com/search/{query}?aid={uuid4()}&type={search_type}"
    if request.source is BrowseSource.USER:
        return f"https://www.douyin.com/user/{quote(request.user_key or '', safe='')}"
    return "https://www.douyin.com/jingxuan"


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


def _build_telegram_source_url(request: BrowsePostsInput) -> str:
    key = (request.user_key or "").strip().lstrip("@")
    return f"https://web.telegram.org/a/#@{quote(key, safe='')}"


def _normalize_telegram_source_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host == "web.telegram.org":
        return value
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise CrawlerError(ErrorCode.INVALID_REQUEST, "Telegram 地址缺少频道或群组标识。")
    if parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
        return f"https://web.telegram.org/a/#-100{parts[1]}"
    return f"https://web.telegram.org/a/#@{quote(parts[0].lstrip('@'), safe='')}"


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
    if platform is BrowsePlatform.TELEGRAM:
        if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            return None
        private_match = _TELEGRAM_PRIVATE_POST_PATH.match(parsed.path)
        if private_match:
            channel_id, post_id = private_match.groups()
            return f"https://t.me/c/{channel_id}/{post_id}", post_id, channel_id, None
        public_match = _TELEGRAM_PUBLIC_POST_PATH.match(parsed.path)
        if not public_match:
            return None
        handle, post_id = public_match.groups()
        return f"https://t.me/{handle}/{post_id}", post_id, handle, handle
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


def _extract_rows(page: Page, request: BrowsePostsInput) -> list[dict[str, Any]]:
    platform = request.platform
    if platform is BrowsePlatform.X:
        return _extract_x_rows(page)
    if platform is BrowsePlatform.DOUYIN:
        return _extract_douyin_rows(page)
    if platform is BrowsePlatform.XIAOHONGSHU:
        return _extract_xhs_rows(page)
    return _extract_telegram_rows(page, request)


def _extract_x_rows(page: Page) -> list[dict[str, Any]]:
    return page.locator(_POST_SELECTORS[BrowsePlatform.X]).evaluate_all(
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
    return page.locator(_POST_SELECTORS[BrowsePlatform.DOUYIN]).evaluate_all(
        r"""
        (nodes) => nodes.map((node) => {
          const waterfall = node.matches('[id^="waterfall_item_"]')
            ? node
            : node.closest('[id^="waterfall_item_"]');
          const waterfallId = (waterfall?.id || '').replace(/^waterfall_item_/, '');
          const awemeId = (node.getAttribute('data-aweme-id') || waterfallId || '').trim();
          const href = node.getAttribute('href') || (awemeId ? `/video/${awemeId}` : '');
          if (!/^\/(video|note)\/\d+/.test(new URL(href, location.origin).pathname)) return null;
          const card = node.closest(
            '[data-aweme-id], [id^="waterfall_item_"], .search-result-card, .discover-video-card-item, li, article, [data-e2e*="feed"], [data-e2e*="search"]'
          ) || node.parentElement;
          const authorLink = card?.querySelector('a[href*="/user/"]');
          const titleNode = card?.querySelector('[data-e2e*="desc"], [class*="title"], [class*="desc"], h1, h2, h3');
          return {
            url: new URL(href, 'https://www.douyin.com').href,
            author_id: authorLink ? new URL(authorLink.getAttribute('href'), location.origin).pathname.split('/user/')[1]?.split('/')[0] : null,
            author_name: authorLink?.textContent?.trim() || null,
            text: node.getAttribute('title') || titleNode?.textContent?.trim() || node.getAttribute('aria-label') || card?.innerText?.trim() || null,
            published_at: card?.querySelector('time[datetime]')?.getAttribute('datetime') || null,
            likes: card?.querySelector('[data-e2e*="like"]')?.textContent?.trim() || null,
            has_image: Boolean(card?.querySelector('img')),
            has_video: true,
          };
        }).filter(Boolean)
        """
    )


def _extract_xhs_rows(page: Page) -> list[dict[str, Any]]:
    return page.locator(_POST_SELECTORS[BrowsePlatform.XIAOHONGSHU]).evaluate_all(
        r"""
        (links) => {
        const seen = new Set();
        return links.map((link) => {
          const rawHref = link.getAttribute('href') || '';
          const rawPath = new URL(rawHref, location.origin).pathname;
          const postId = rawPath.match(/^\/(?:explore|discovery\/item)\/([A-Za-z0-9]+)/)?.[1];
          const container = link.parentElement;
          const authenticatedLink = postId ? [...(container?.querySelectorAll('a[href]') || [])]
            .find((candidate) => {
              const candidateUrl = new URL(candidate.getAttribute('href') || '', location.origin);
              return candidateUrl.pathname.match(
                new RegExp(`^/(?:explore|discovery/item|search_result)/${postId}$`)
              ) && candidateUrl.searchParams.has('xsec_token');
            }) : null;
          const detailLink = authenticatedLink || link;
          const href = detailLink.getAttribute('href') || rawHref;
          const path = new URL(href, location.origin).pathname;
          const identity = path.match(
            /^\/(?:explore|discovery\/item|search_result)\/([A-Za-z0-9]+)/
          )?.[1];
          if (!identity || seen.has(identity)) return null;
          seen.add(identity);
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
        }).filter(Boolean);
        }
        """
    )


def _extract_telegram_rows(page: Page, request: BrowsePostsInput) -> list[dict[str, Any]]:
    rows = page.locator(_POST_SELECTORS[BrowsePlatform.TELEGRAM]).evaluate_all(
        r"""
        (messages) => messages.map((message) => {
          const messageId = message.getAttribute('data-message-id') ||
            (message.id || '').replace(/^message-/, '');
          const text = message.querySelector('.text-content')?.textContent?.trim() ||
            message.querySelector('.message-content')?.textContent?.trim() || null;
          const meta = message.querySelector('.MessageMeta')?.textContent?.trim() || null;
          const views = message.querySelector('.MessageMeta .message-views, [class*="views"]')?.textContent?.trim() || meta;
          const media = [...message.querySelectorAll('.media-inner')];
          const isVideo = (node) => Boolean(node.querySelector(
            'video, .message-media-duration, .icon-large-play, [class*="video"]'
          ));
          return {
            message_id: messageId,
            text,
            views,
            // Telegram Web A initially paints media into low-resolution canvas
            // placeholders and portals the full image/video into a sibling
            // layer. Presence of a canvas still means this is a media post.
            has_image: media.some((node) => !isVideo(node) && Boolean(
              node.querySelector('img.full-media, img[src^="blob:"], canvas')
            )),
            has_video: media.some(isVideo),
          };
        }).filter((row) => /^\d+$/.test(row.message_id || ''))
        """
    )
    prefix, author_id, author_handle = _telegram_post_prefix(request, page.url)
    normalized = []
    # DOM order can differ during virtual-list transitions and grouped updates.
    for row in sorted(rows, key=lambda item: int(item["message_id"]), reverse=True):
        if request.view is BrowseView.MEDIA and not (row.get("has_image") or row.get("has_video")):
            continue
        item = dict(row)
        item["url"] = f"{prefix}/{item.pop('message_id')}"
        item["author_id"] = author_id
        item["author_name"] = author_handle
        normalized.append(item)
    return normalized


def _telegram_post_prefix(
    request: BrowsePostsInput,
    current_url: str,
) -> tuple[str, str | None, str | None]:
    if request.source is BrowseSource.USER and request.user_key:
        handle = request.user_key.strip().lstrip("@")
        return f"https://t.me/{handle}", handle, handle
    if request.start_url is not None:
        parsed = urlsplit(str(request.start_url))
        host = (parsed.hostname or "").lower()
        parts = [part for part in parsed.path.split("/") if part]
        if host in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"} and parts:
            if parts[0] == "c" and len(parts) >= 2:
                return f"https://t.me/c/{parts[1]}", parts[1], None
            handle = parts[0].lstrip("@")
            return f"https://t.me/{handle}", handle, handle
    fragment = urlsplit(current_url).fragment
    if fragment.startswith("-100") and fragment[4:].split("_")[0].isdigit():
        channel_id = fragment[4:].split("_")[0]
        return f"https://t.me/c/{channel_id}", channel_id, None
    raise CrawlerError(
        ErrorCode.BROWSE_FAILED,
        "无法确定 Telegram 频道标识，请使用 t.me 频道地址或频道用户名。",
    )


def _wait_for_initial_posts(page: Page, request: BrowsePostsInput) -> None:
    """Wait for asynchronously hydrated result cards before declaring a page empty.

    Douyin's search shell reaches ``domcontentloaded`` several seconds before its
    waterfall cards are attached. The scrolling loop used to count those empty
    checks as stagnant rounds and exit before the search response was rendered.
    """
    timeout_ms = min(
        int(request.navigation_timeout_seconds * 1_000),
        max(8_000, request.settle_after_scroll_ms * 6),
    )
    try:
        page.wait_for_selector(
            _POST_SELECTORS[request.platform],
            state="attached",
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError:
        # Empty searches and platform-side loading failures are reported through
        # the existing warning after the normal extraction pass.
        return


def _wait_for_restored_tabs(context: Any) -> None:
    """Give BitBrowser half a second to restore its configured/history tabs."""
    pages = [page for page in context.pages if not page.is_closed()]
    if pages:
        pages[-1].wait_for_timeout(500)
    else:
        sleep(0.5)


def _existing_platform_page(pages: list[Page], platform: BrowsePlatform) -> Page | None:
    domains = {
        BrowsePlatform.X: ("x.com", "twitter.com"),
        BrowsePlatform.DOUYIN: ("douyin.com",),
        BrowsePlatform.XIAOHONGSHU: ("xiaohongshu.com",),
        BrowsePlatform.TELEGRAM: ("web.telegram.org",),
    }[platform]
    candidates: list[Page] = []
    for page in pages:
        if page.is_closed():
            continue
        host = (urlsplit(page.url).hostname or "").lower().rstrip(".")
        if any(host == domain or host.endswith(f".{domain}") for domain in domains):
            candidates.append(page)
    return candidates[-1] if candidates else None


def _same_navigation_url(current: str, target: str) -> bool:
    return current.rstrip("/") == target.rstrip("/")


def _raise_if_login_required(page: Page, platform: BrowsePlatform) -> None:
    path = urlsplit(page.url).path.lower()
    login_paths = {
        BrowsePlatform.X: ("/i/flow/login", "/login"),
        BrowsePlatform.DOUYIN: ("/passport/login",),
        BrowsePlatform.XIAOHONGSHU: ("/login",),
        BrowsePlatform.TELEGRAM: ("/auth", "/login"),
    }[platform]
    if any(path.startswith(prefix) for prefix in login_paths):
        raise CrawlerError(
            ErrorCode.SESSION_REAUTH_REQUIRED,
            f"{_PLATFORM_LABEL[platform]} 登录会话已失效，请在对应比特浏览器 Profile 中重新登录。",
        )


def _platform_challenge_visible(page: Page) -> bool:
    """Detect visible verification UI without attempting to solve it."""
    try:
        title = page.title().strip().lower()
    except Exception:
        title = ""
    if any(marker in title for marker in ("验证码", "安全验证", "captcha", "verify")):
        return True

    try:
        if page.locator(_CHALLENGE_SELECTORS).first.is_visible(timeout=300):
            return True
    except Exception:
        pass

    try:
        body_text = page.locator("body").inner_text(timeout=500)
    except Exception:
        body_text = ""
    return any(marker in body_text for marker in _CHALLENGE_TEXT_MARKERS)


def _raise_if_platform_challenge(
    page: Page,
    platform: BrowsePlatform,
    *,
    wait_timeout_ms: int = 0,
) -> None:
    if not _platform_challenge_visible(page):
        return

    deadline = monotonic() + max(0, wait_timeout_ms) / 1_000
    while monotonic() < deadline:
        remaining_ms = max(1, int((deadline - monotonic()) * 1_000))
        page.wait_for_timeout(min(1_000, remaining_ms))
        if not _platform_challenge_visible(page):
            # Verification overlays can disappear briefly while reloading. Wait
            # once more before resuming extraction to avoid reading the old DOM.
            page.wait_for_timeout(500)
            if not _platform_challenge_visible(page):
                return

    raise CrawlerError(
        ErrorCode.PLATFORM_UNAVAILABLE,
        (
            f"{_PLATFORM_LABEL[platform]} 弹出了图片或安全验证，等待手动处理已超时。"
            "请在对应比特浏览器窗口完成验证后重试；Agent 不会自动破解验证码。"
        ),
        retryable=True,
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
