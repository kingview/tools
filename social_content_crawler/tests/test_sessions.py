from __future__ import annotations

import json
import os
from pathlib import Path
from stat import S_IMODE

import pytest

import social_content_crawler.sessions as sessions_module
from social_content_crawler.errors import CrawlerError, ErrorCode
from social_content_crawler.sessions import (
    BrowserProfile,
    BitBrowserClient,
    SessionRegistry,
    _proxy_url_from_profile_detail,
    platform_from_profile_name,
    validate_loopback_api_url,
)


PROFILE_ID = "profile-x-001"
AUTH_COOKIES = [
    {
        "domain": ".x.com",
        "name": "auth_token",
        "value": "secret-auth-token",
        "path": "/",
        "secure": True,
        "httpOnly": True,
    },
    {
        "domain": ".x.com",
        "name": "ct0",
        "value": "secret-csrf-token",
        "path": "/",
        "secure": True,
    },
    {
        "domain": ".example.com",
        "name": "unrelated",
        "value": "must-not-be-exported",
        "path": "/",
    },
]


def _transport(path: str, payload: dict):
    if path == "/health":
        return {"success": True, "data": None}
    if path == "/browser/list":
        return {
            "success": True,
            "data": {"list": [{"id": PROFILE_ID, "name": "X 工作账号 01"}]},
        }
    if path == "/browser/detail":
        assert payload == {"id": PROFILE_ID}
        return {
            "success": True,
            "data": {
                "id": PROFILE_ID,
                "name": "X 工作账号 01",
                "cookie": json.dumps(AUTH_COOKIES),
            },
        }
    if path == "/browser/pids/alive":
        assert payload == {"ids": [PROFILE_ID]}
        return {"success": True, "data": {}}
    if path == "/browser/open":
        assert payload == {"id": PROFILE_ID, "loadExtensions": False}
        return {
            "success": True,
            "data": {
                "ws": "ws://127.0.0.1:50106/devtools/browser/test",
                "http": "127.0.0.1:50106",
            },
        }
    raise AssertionError(path)


def _registry(tmp_path: Path) -> SessionRegistry:
    return SessionRegistry(
        tmp_path / "sessions.json",
        client_factory=lambda api_url: BitBrowserClient(api_url, transport=_transport),
    )


def test_bitbrowser_client_lists_profiles() -> None:
    client = BitBrowserClient("http://127.0.0.1:54345", transport=_transport)
    client.health()
    assert client.list_profiles()[0].profile_id == PROFILE_ID
    assert client.open_profile(PROFILE_ID).startswith("ws://127.0.0.1:50106/")


def test_bitbrowser_client_lists_all_profile_pages() -> None:
    pages = []

    def transport(path: str, payload: dict):
        assert path == "/browser/list"
        pages.append(payload["page"])
        start = payload["page"] * 100
        count = 100 if payload["page"] == 0 else 2
        return {
            "success": True,
            "data": {
                "list": [
                    {"id": f"profile-{index}", "name": f"DY-素材-{index:03d}"}
                    for index in range(start, start + count)
                ]
            },
        }

    client = BitBrowserClient("http://127.0.0.1:54345", transport=transport)
    profiles = client.list_all_profiles()
    assert len(profiles) == 102
    assert pages == [0, 1]


@pytest.mark.parametrize(
    ("name", "platform"),
    [
        ("DY-素材-01", "douyin"),
        ("抖音_直播 02", "douyin"),
        ("XHS-美女-01", "xiaohongshu"),
        ("【小红书】品牌账号", "xiaohongshu"),
        ("X-发布-01", "x"),
        ("Twitter 工作账号", "x"),
        ("TG-频道-01", "telegram"),
        ("Telegram: 新闻", "telegram"),
        ("Xavier 的普通窗口", None),
        ("素材-X-01", None),
        ("未分类窗口", None),
    ],
)
def test_profile_name_platform_rules_are_explicit(name: str, platform: str | None) -> None:
    assert platform_from_profile_name(name) == platform


def test_auto_registers_named_profiles_without_rotating_existing_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles = [
        BrowserProfile("dy-1", "DY-素材-01"),
        BrowserProfile("xhs-1", "小红书-品牌-01"),
        BrowserProfile("x-1", "X-发布-01"),
        BrowserProfile("tg-1", "TG-频道-01"),
        BrowserProfile("other", "普通窗口"),
    ]

    class FakeClient:
        def __init__(self, api_url):
            self.api_url = validate_loopback_api_url(api_url)

        def health(self):
            pass

        def list_all_profiles(self):
            return profiles

        def profile_detail(self, profile_id):
            profile = next(item for item in profiles if item.profile_id == profile_id)
            return {"name": profile.name}

    monkeypatch.setattr(sessions_module, "_validate_profile_login", lambda *_args: None)
    registry = SessionRegistry(tmp_path / "sessions.json", client_factory=FakeClient)
    existing = registry.register_bitbrowser("douyin", "http://127.0.0.1:54345", "dy-1")

    report = registry.auto_register_named_profiles("http://127.0.0.1:54345")

    assert report.discovered == 5
    assert [item.session_ref for item in report.existing] == [existing.session_ref]
    assert {item.platform for item in report.registered} == {"xiaohongshu", "x", "telegram"}
    assert [item.name for item in report.unmatched] == ["普通窗口"]
    assert report.errors == ()
    assert len(registry.list()) == 4


def test_bitbrowser_client_reuses_running_profile_without_opening_again() -> None:
    calls: list[str] = []

    def transport(path: str, payload: dict):
        calls.append(path)
        if path == "/browser/pids/alive":
            assert payload == {"ids": [PROFILE_ID]}
            return {"success": True, "data": {PROFILE_ID: 43210}}
        if path == "/browser/ports":
            assert payload == {}
            return {"success": True, "data": {PROFILE_ID: "50106"}}
        raise AssertionError(f"window should not be reopened: {path}")

    client = BitBrowserClient("http://127.0.0.1:54345", transport=transport)

    assert client.open_profile(PROFILE_ID) == "http://127.0.0.1:50106"
    assert calls == ["/browser/pids/alive", "/browser/ports"]


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com:54345",
        "http://127.0.0.1",
        "http://user:pass@127.0.0.1:54345",
        "http://127.0.0.1:54345/api",
    ],
)
def test_bitbrowser_api_must_be_loopback(value: str) -> None:
    with pytest.raises(CrawlerError) as raised:
        validate_loopback_api_url(value)
    assert raised.value.code is ErrorCode.INVALID_REQUEST


def test_registry_stores_only_opaque_profile_reference(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    record = registry.register_bitbrowser_x("http://127.0.0.1:54345", PROFILE_ID)

    assert record.session_ref.startswith("sess_x_")
    stored = registry.path.read_text(encoding="utf-8")
    assert "secret-auth-token" not in stored
    assert "secret-csrf-token" not in stored
    assert "cookie" not in stored.lower()
    if os.name != "nt":
        assert S_IMODE(registry.path.stat().st_mode) == 0o600


def test_session_cookiefile_is_x_only_and_always_deleted(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    record = registry.register_bitbrowser_x("http://127.0.0.1:54345", PROFILE_ID)

    cookiefile: Path | None = None
    with registry.materialize_cookiefile(
        record.session_ref,
        ["https://x.com/example/status/1"],
        tmp_path,
    ) as materialized:
        cookiefile = materialized
        text = materialized.read_text(encoding="utf-8")
        assert "secret-auth-token" in text
        assert "secret-csrf-token" in text
        assert "must-not-be-exported" not in text
        if os.name != "nt":
            assert S_IMODE(materialized.stat().st_mode) == 0o600
    assert cookiefile is not None and not cookiefile.exists()


def test_profile_proxy_is_encoded_for_downloader_without_logging_parts() -> None:
    proxy_url = _proxy_url_from_profile_detail(
        {
            "proxyType": "http",
            "host": "proxy.example.com",
            "port": 8080,
            "proxyUserName": "team user",
            "proxyPassword": "p@ss/word",
        }
    )
    assert proxy_url == "http://team%20user:p%40ss%2Fword@proxy.example.com:8080"
    assert _proxy_url_from_profile_detail({"proxyType": "noproxy"}) is None


def test_download_session_requires_profile_proxy_and_cleans_cookiefile(tmp_path: Path) -> None:
    def proxy_transport(path: str, payload: dict):
        response = _transport(path, payload)
        if path == "/browser/detail":
            response["data"].update(
                {
                    "proxyType": "socks5",
                    "host": "127.0.0.1",
                    "port": 1080,
                    "proxyUserName": "proxy-user",
                    "proxyPassword": "proxy-password",
                }
            )
        return response

    registry = SessionRegistry(
        tmp_path / "proxy.sessions.json",
        client_factory=lambda api_url: BitBrowserClient(api_url, transport=proxy_transport),
    )
    record = registry.register_bitbrowser_x("http://127.0.0.1:54345", PROFILE_ID)
    cookiefile = None
    with registry.materialize_download_session(
        record.session_ref,
        ["https://x.com/example/status/1"],
        tmp_path,
    ) as materialized:
        cookiefile = materialized.cookiefile
        assert materialized.proxy_url == "socks5://proxy-user:proxy-password@127.0.0.1:1080"
        assert materialized.cookiefile.is_file()
    assert cookiefile is not None and not cookiefile.exists()


def test_download_session_allows_profile_without_proxy(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    record = registry.register_bitbrowser_x("http://127.0.0.1:54345", PROFILE_ID)
    with registry.materialize_download_session(
        record.session_ref,
        ["https://x.com/example/status/1"],
        tmp_path,
    ) as materialized:
        assert materialized.proxy_url is None
        assert materialized.cookiefile.is_file()


def test_x_session_cannot_be_used_for_another_platform(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    record = registry.register_bitbrowser_x("http://127.0.0.1:54345", PROFILE_ID)
    with pytest.raises(CrawlerError) as raised:
        with registry.materialize_cookiefile(
            record.session_ref,
            ["https://www.youtube.com/watch?v=1"],
            tmp_path,
        ):
            pass
    assert raised.value.code is ErrorCode.INVALID_REQUEST


def test_registration_requires_a_logged_in_x_profile(tmp_path: Path) -> None:
    def anonymous_transport(path: str, payload: dict):
        response = _transport(path, payload)
        if path == "/browser/detail":
            response["data"]["cookie"] = "[]"
        return response

    registry = SessionRegistry(
        tmp_path / "sessions.json",
        client_factory=lambda api_url: BitBrowserClient(api_url, transport=anonymous_transport),
    )
    with pytest.raises(CrawlerError) as raised:
        registry.register_bitbrowser_x("http://127.0.0.1:54345", PROFILE_ID)
    assert raised.value.code is ErrorCode.SESSION_REAUTH_REQUIRED


@pytest.mark.parametrize(
    ("platform", "domain", "cookie_name", "prefix"),
    [
        ("douyin", ".douyin.com", "sessionid", "sess_douyin_"),
        ("xiaohongshu", ".xiaohongshu.com", "web_session", "sess_xhs_"),
    ],
)
def test_registers_mainland_platform_sessions(
    tmp_path: Path,
    platform: str,
    domain: str,
    cookie_name: str,
    prefix: str,
) -> None:
    def transport(path: str, payload: dict):
        if path == "/browser/detail":
            return {
                "success": True,
                "data": {
                    "id": PROFILE_ID,
                    "name": f"{platform}-profile",
                    "cookie": json.dumps(
                        [
                            {
                                "domain": domain,
                                "name": cookie_name,
                                "value": "platform-secret",
                                "path": "/",
                                "secure": True,
                            }
                        ]
                    ),
                },
            }
        raise AssertionError(path)

    registry = SessionRegistry(
        tmp_path / f"{platform}.sessions.json",
        client_factory=lambda api_url: BitBrowserClient(api_url, transport=transport),
    )
    record = registry.register_bitbrowser(platform, "http://127.0.0.1:54345", PROFILE_ID)

    assert record.session_ref.startswith(prefix)
    assert registry.validate_session(record.session_ref, platform) == record
    assert "platform-secret" not in registry.path.read_text(encoding="utf-8")


def test_registration_uses_live_cookies_when_open_profile_snapshot_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stale_transport(path: str, payload: dict):
        response = _transport(path, payload)
        if path == "/browser/detail":
            response["data"]["cookie"] = "[]"
        return response

    monkeypatch.setattr(
        sessions_module,
        "_read_live_profile_cookies",
        lambda endpoint: [
            {
                "domain": ".xiaohongshu.com",
                "name": "web_session",
                "value": "live-only-secret",
                "path": "/",
                "secure": True,
            }
        ],
    )
    registry = SessionRegistry(
        tmp_path / "live.sessions.json",
        client_factory=lambda api_url: BitBrowserClient(api_url, transport=stale_transport),
    )

    record = registry.register_bitbrowser(
        "xiaohongshu", "http://127.0.0.1:54345", PROFILE_ID
    )

    assert record.session_ref.startswith("sess_xhs_")
    assert registry.validate_session(record.session_ref, "xiaohongshu") == record
    assert "live-only-secret" not in registry.path.read_text(encoding="utf-8")


def test_registers_telegram_from_live_web_session_without_exporting_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions_module, "_require_telegram_web_login", lambda endpoint: None)
    registry = _registry(tmp_path)

    record = registry.register_bitbrowser(
        "telegram", "http://127.0.0.1:54345", PROFILE_ID
    )

    assert record.session_ref.startswith("sess_telegram_")
    assert registry.validate_session(record.session_ref, "telegram") == record
    stored = registry.path.read_text(encoding="utf-8")
    assert "secret-auth-token" not in stored


class _TelegramLocator:
    def __init__(self, visible: bool) -> None:
        self._visible = visible

    @property
    def first(self):
        return self

    def count(self) -> int:
        return int(self._visible)

    def nth(self, index: int):
        return self

    def is_visible(self, *, timeout: int) -> bool:
        return self._visible


class _TelegramPage:
    def __init__(
        self,
        *,
        ready_after: int | None = 1,
        logged_out: bool = False,
        visible_selectors: set[str] | None = None,
    ) -> None:
        self.url = "https://web.telegram.org/a/#@example"
        self.ready_after = ready_after
        self.logged_out = logged_out
        self.visible_selectors = visible_selectors or set()
        self.polls = 0

    def is_closed(self) -> bool:
        return False

    def locator(self, selector: str) -> _TelegramLocator:
        if selector == "#auth-pages":
            self.polls += 1
        if self.logged_out and selector == "#auth-pages":
            return _TelegramLocator(True)
        ready = self.ready_after is not None and self.polls >= self.ready_after
        return _TelegramLocator(
            selector in self.visible_selectors or (ready and selector == ".chat-list")
        )


class _TelegramBrowser:
    def __init__(self, *pages: _TelegramPage) -> None:
        self.contexts = [type("Context", (), {"pages": list(pages)})()]


def test_telegram_login_waits_for_restored_web_app() -> None:
    page = _TelegramPage(ready_after=3)

    sessions_module._wait_for_telegram_web_login(
        _TelegramBrowser(page), timeout_seconds=0.2, poll_interval_seconds=0.001
    )

    assert page.polls >= 3


def test_telegram_login_accepts_any_logged_in_tab() -> None:
    logged_out = _TelegramPage(ready_after=None, logged_out=True)
    logged_in = _TelegramPage(ready_after=1)

    sessions_module._wait_for_telegram_web_login(
        _TelegramBrowser(logged_out, logged_in), timeout_seconds=0
    )


def test_telegram_login_accepts_mounted_shell_when_chat_list_is_hidden() -> None:
    page = _TelegramPage(
        ready_after=None,
        visible_selectors={"#LeftColumn", "#MiddleColumn"},
    )

    sessions_module._wait_for_telegram_web_login(
        _TelegramBrowser(page), timeout_seconds=0
    )


def test_telegram_login_rejects_explicit_logged_out_page() -> None:
    with pytest.raises(CrawlerError) as raised:
        sessions_module._wait_for_telegram_web_login(
            _TelegramBrowser(_TelegramPage(ready_after=None, logged_out=True)),
            timeout_seconds=0,
        )

    assert raised.value.code is ErrorCode.SESSION_REAUTH_REQUIRED


def test_telegram_loading_page_is_not_reported_as_logged_out() -> None:
    with pytest.raises(CrawlerError, match="页面加载未完成") as raised:
        sessions_module._wait_for_telegram_web_login(
            _TelegramBrowser(_TelegramPage(ready_after=None)), timeout_seconds=0,
        )
    assert raised.value.code is ErrorCode.PLATFORM_UNAVAILABLE


def test_telegram_missing_tab_does_not_require_reregistration() -> None:
    with pytest.raises(CrawlerError, match="未发现 Telegram Web 标签") as raised:
        sessions_module._wait_for_telegram_web_login(_TelegramBrowser(), timeout_seconds=0)
    assert raised.value.code is ErrorCode.PLATFORM_UNAVAILABLE


def test_telegram_loading_tab_takes_precedence_over_old_login_tab() -> None:
    with pytest.raises(CrawlerError) as raised:
        sessions_module._wait_for_telegram_web_login(
            _TelegramBrowser(_TelegramPage(logged_out=True), _TelegramPage(ready_after=None)),
            timeout_seconds=0,
        )
    assert raised.value.code is ErrorCode.PLATFORM_UNAVAILABLE


def test_telegram_wait_pumps_browser_events() -> None:
    page = _TelegramPage(ready_after=None)
    def restored(milliseconds):
        page.ready_after = 1
    page.wait_for_timeout = restored
    sessions_module._wait_for_telegram_web_login(
        _TelegramBrowser(page), timeout_seconds=.1, poll_interval_seconds=.001,
    )


def test_bitbrowser_api_bypasses_proxy_and_waits_longer_for_open(monkeypatch) -> None:
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"success":true,"data":{}}'
    class Opener:
        def open(self, request, timeout):
            calls.append(timeout)
            return Response()
    def opener(handler):
        assert handler.proxies == {}
        return Opener()
    monkeypatch.setattr(sessions_module, 'build_opener', opener)
    client = BitBrowserClient('http://127.0.0.1:54345')
    client._post('/health', {})
    client._post('/browser/open', {'id':PROFILE_ID})
    assert calls == [8.0, 30.0]


@pytest.mark.parametrize('blank, expected_reloads', [(True, 1), (False, 0)])
def test_telegram_recovers_only_stalled_blank_tabs(monkeypatch, blank, expected_reloads) -> None:
    from itertools import count
    clock = count(0, .5)
    monkeypatch.setattr(sessions_module, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(sessions_module, 'sleep', lambda seconds: None)
    page = _TelegramPage(ready_after=None)
    page.evaluate = lambda script: blank
    calls = []
    def reload(**kwargs):
        calls.append(kwargs)
        page.ready_after = 1
    page.reload = reload
    if blank:
        sessions_module._wait_for_telegram_web_login(_TelegramBrowser(page), timeout_seconds=45)
    else:
        with pytest.raises(CrawlerError):
            sessions_module._wait_for_telegram_web_login(_TelegramBrowser(page), timeout_seconds=45)
    assert len(calls) == expected_reloads


def test_telegram_blank_recovery_is_not_repeated(monkeypatch) -> None:
    from itertools import count
    clock = count(0, .5)
    monkeypatch.setattr(sessions_module, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(sessions_module, 'sleep', lambda seconds: None)
    page = _TelegramPage(ready_after=None)
    page.evaluate = lambda script: True
    calls = []
    page.reload = lambda **kwargs: calls.append(kwargs)
    with pytest.raises(CrawlerError):
        sessions_module._wait_for_telegram_web_login(_TelegramBrowser(page), timeout_seconds=45)
    assert len(calls) == 1
