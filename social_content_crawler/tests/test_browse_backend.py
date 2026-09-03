from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from social_content_crawler.browse_backend import (
    XPostBrowserBackend,
    _extract_douyin_rows,
    _extract_telegram_rows,
    _existing_platform_page,
    _raise_if_platform_challenge,
    _wait_for_initial_posts,
    _metric_number,
    build_source_url,
    build_x_source_url,
    normalize_rows,
)
from social_content_crawler.browse_contracts import BrowsePostsInput
from social_content_crawler.browse_contracts import BrowsePlatform
from social_content_crawler.errors import CrawlerError, ErrorCode


SESSION_REF = "sess_x_abcdefghijklmnopqrstuvwx"


def _request(**overrides) -> BrowsePostsInput:
    payload = {
        "platform": "x",
        "session_ref": SESSION_REF,
        "source": "search",
        "view": "latest",
        "query": "open source",
        "max_items": 10,
    }
    payload.update(overrides)
    return BrowsePostsInput(**payload)


def test_builds_x_search_and_user_urls_without_arbitrary_hosts() -> None:
    assert build_x_source_url(_request()) == (
        "https://x.com/search?q=open+source&src=typed_query&f=live"
    )
    assert build_x_source_url(
        _request(source="search", view="media", query="AI 视频")
    ) == "https://x.com/search?q=AI+%E8%A7%86%E9%A2%91&src=typed_query&f=media"
    assert build_x_source_url(
        _request(source="user", view="replies", query=None, user_key="OpenAI")
    ) == "https://x.com/OpenAI/with_replies"


def test_normalizes_and_deduplicates_structured_posts() -> None:
    rows = [
        {
            "url": "https://x.com/example/status/123/photo/1",
            "author_name": "Example Author",
            "text": "  Hello   world  ",
            "language": "en",
            "published_at": "2026-08-23T10:20:30Z",
            "replies": "12 Replies. Reply",
            "reposts": "1.2K Reposts. Repost",
            "likes": "3万 Likes. Like",
            "views": "1.5M Views",
            "has_image": True,
            "has_video": False,
        },
        {"url": "https://x.com/example/status/123", "text": "duplicate"},
        {"url": "https://example.com/not-x/status/1"},
    ]

    posts = normalize_rows(BrowsePlatform.X, rows, 10)

    assert len(posts) == 1
    assert str(posts[0].url) == "https://x.com/example/status/123"
    assert posts[0].text == "Hello world"
    assert posts[0].media_types == ["image"]
    assert posts[0].metrics.replies == 12
    assert posts[0].metrics.reposts == 1_200
    assert posts[0].metrics.likes == 30_000
    assert posts[0].metrics.views == 1_500_000


def test_backend_resolves_session_opens_profile_and_collects() -> None:
    calls = []

    class FakeRegistry:
        def validate_session(self, session_ref, platform):
            assert session_ref == SESSION_REF
            assert platform == "x"
            return SimpleNamespace(api_url="http://127.0.0.1:54345", profile_id="profile-1")

    class FakeClient:
        def __init__(self, api_url):
            assert api_url == "http://127.0.0.1:54345"

        def open_profile(self, profile_id):
            assert profile_id == "profile-1"
            return "ws://127.0.0.1:50106/devtools/browser/test"

    class FakeAutomation:
        def collect(self, *, cdp_endpoint, source_url, request):
            calls.append((cdp_endpoint, source_url, request.session_ref))
            return ([{"url": "https://x.com/author/status/456", "has_video": True}], False, [])

    backend = XPostBrowserBackend(
        session_registry=FakeRegistry(),
        automation=FakeAutomation(),
        client_factory=FakeClient,
    )
    output = backend.run(_request())

    assert output.posts[0].post_id == "456"
    assert output.posts[0].media_types == ["video"]
    assert calls[0][0].startswith("ws://127.0.0.1:")


def test_builds_douyin_and_xiaohongshu_routes() -> None:
    douyin_search = BrowsePostsInput(
        platform="douyin",
        session_ref="sess_douyin_abcdefghijklmnopqrstuvwx",
        source="search",
        view="media",
        query="本地大模型",
    )
    xhs_user = BrowsePostsInput(
        platform="xiaohongshu",
        session_ref="sess_xhs_abcdefghijklmnopqrstuvwx",
        source="user",
        view="posts",
        user_key="user123",
    )
    douyin_url = urlsplit(build_source_url(douyin_search))
    douyin_query = parse_qs(douyin_url.query)
    assert douyin_url.path == "/search/%E6%9C%AC%E5%9C%B0%E5%A4%A7%E6%A8%A1%E5%9E%8B"
    assert douyin_query["type"] == ["video"]
    assert len(douyin_query["aid"][0]) == 36
    assert build_source_url(xhs_user) == (
        "https://www.xiaohongshu.com/user/profile/user123"
    )


def test_builds_douyin_recommendation_feed_route() -> None:
    request = BrowsePostsInput(
        platform="douyin",
        session_ref="sess_douyin_abcdefghijklmnopqrstuvwx",
        source="timeline",
        view="top",
        max_items=1,
    )

    assert build_source_url(request) == "https://www.douyin.com/jingxuan"


def test_douyin_collector_includes_new_aweme_id_cards() -> None:
    captured = {}

    class FakeLocator:
        def evaluate_all(self, script):
            captured["script"] = script
            return [{"url": "https://www.douyin.com/video/7679489315499543859"}]

    class FakePage:
        def locator(self, selector):
            captured["selector"] = selector
            return FakeLocator()

    rows = _extract_douyin_rows(FakePage())

    assert "[data-aweme-id]" in captured["selector"]
    assert "waterfall_item_" in captured["selector"]
    assert "data-aweme-id" in captured["script"]
    assert "waterfall_item_" in captured["script"]
    assert rows[0]["url"].endswith("/video/7679489315499543859")


def test_waits_for_douyin_waterfall_cards_before_extracting() -> None:
    captured = {}

    class FakePage:
        def wait_for_selector(self, selector, *, state, timeout):
            captured.update(selector=selector, state=state, timeout=timeout)

    request = BrowsePostsInput(
        platform="douyin",
        session_ref="sess_douyin_abcdefghijklmnopqrstuvwx",
        source="search",
        view="top",
        query="美女",
        navigation_timeout_seconds=30,
        settle_after_scroll_ms=900,
    )

    _wait_for_initial_posts(FakePage(), request)

    assert "waterfall_item_" in captured["selector"]
    assert captured["state"] == "attached"
    assert captured["timeout"] == 8_000


def test_reuses_existing_douyin_tab_instead_of_unrelated_tab() -> None:
    class FakePage:
        def __init__(self, url):
            self.url = url

        def is_closed(self):
            return False

    unrelated = FakePage("https://example.com/")
    douyin = FakePage("https://www.douyin.com/")

    assert _existing_platform_page(
        [douyin, unrelated],
        BrowsePlatform.DOUYIN,
    ) is douyin


def test_waits_for_manual_image_verification_then_continues() -> None:
    class FakeChallengeLocator:
        def __init__(self, page):
            self.page = page

        @property
        def first(self):
            return self

        def is_visible(self, *, timeout):
            assert timeout == 300
            self.page.challenge_checks += 1
            return self.page.challenge_checks == 1

    class FakeBodyLocator:
        def inner_text(self, *, timeout):
            assert timeout == 500
            return "搜索结果"

    class FakePage:
        challenge_checks = 0

        def title(self):
            return "抖音搜索"

        def locator(self, selector):
            return FakeBodyLocator() if selector == "body" else FakeChallengeLocator(self)

        def wait_for_timeout(self, milliseconds):
            assert milliseconds > 0

    _raise_if_platform_challenge(
        FakePage(),
        BrowsePlatform.DOUYIN,
        wait_timeout_ms=1_000,
    )


def test_unresolved_image_verification_is_retryable() -> None:
    class FakeLocator:
        @property
        def first(self):
            return self

        def is_visible(self, *, timeout):
            return True

    class FakePage:
        def title(self):
            return "抖音搜索"

        def locator(self, selector):
            return FakeLocator()

    try:
        _raise_if_platform_challenge(FakePage(), BrowsePlatform.DOUYIN)
    except CrawlerError as exc:
        assert exc.code == ErrorCode.PLATFORM_UNAVAILABLE
        assert exc.retryable is True
        assert "完成验证后重试" in str(exc)
    else:
        raise AssertionError("expected a retryable challenge error")


def test_normalizes_douyin_and_xiaohongshu_post_urls() -> None:
    douyin = normalize_rows(
        BrowsePlatform.DOUYIN,
        [{"url": "https://www.douyin.com/note/123456?from=search", "author_id": "sec-user"}],
        10,
    )
    xhs = normalize_rows(
        BrowsePlatform.XIAOHONGSHU,
        [{"url": "https://www.xiaohongshu.com/discovery/item/abc123?xsec_token=redacted&ignored=1"}],
        10,
    )
    assert str(douyin[0].url) == "https://www.douyin.com/video/123456"
    assert douyin[0].author_id == "sec-user"
    assert str(xhs[0].url) == (
        "https://www.xiaohongshu.com/explore/abc123?xsec_token=redacted"
    )

    xhs_search = normalize_rows(
        BrowsePlatform.XIAOHONGSHU,
        [
            {
                "url": (
                    "https://www.xiaohongshu.com/search_result/abc123"
                    "?xsec_token=from-card&xsec_source=pc_search&ignored=1"
                )
            }
        ],
        10,
    )
    assert str(xhs_search[0].url) == (
        "https://www.xiaohongshu.com/explore/abc123"
        "?xsec_token=from-card&xsec_source=pc_search"
    )


def test_xiaohongshu_dom_extractor_prefers_tokenized_detail_link() -> None:
    class FakeLocator:
        def evaluate_all(self, script):
            assert "authenticatedLink" in script
            assert "xsec_token" in script
            assert "search_result" in script
            return []

    class FakePage:
        def locator(self, selector):
            assert "/explore/" in selector
            return FakeLocator()

    from social_content_crawler.browse_backend import _extract_xhs_rows

    assert _extract_xhs_rows(FakePage()) == []


def test_metric_parser_supports_compact_and_chinese_units() -> None:
    assert _metric_number("4.2K Likes") == 4_200
    assert _metric_number("2万") == 20_000
    assert _metric_number("1.5亿") == 150_000_000
    assert _metric_number(None) is None


def test_builds_and_extracts_telegram_channel_messages_newest_first() -> None:
    request = BrowsePostsInput(
        platform="telegram",
        session_ref="sess_telegram_abcdefghijklmnopqrstuvwx",
        source="url",
        view="posts",
        start_url="https://t.me/weme_download",
        max_items=10,
    )
    assert build_source_url(request) == "https://web.telegram.org/a/#@weme_download"

    class FakeLocator:
        def evaluate_all(self, script):
            assert "data-message-id" in script
            return [
                {"message_id": "10", "text": "older", "has_image": True},
                {"message_id": "11", "text": "newer", "has_video": True},
            ]

    class FakePage:
        url = "https://web.telegram.org/a/#-1001634371164"

        def locator(self, selector):
            assert "Message" in selector
            return FakeLocator()

    rows = _extract_telegram_rows(FakePage(), request)
    posts = normalize_rows(BrowsePlatform.TELEGRAM, rows, 10)
    assert [post.post_id for post in posts] == ["11", "10"]
    assert str(posts[0].url) == "https://t.me/weme_download/11"
    assert posts[0].media_types == ["video"]
