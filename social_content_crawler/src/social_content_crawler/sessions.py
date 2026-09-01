from __future__ import annotations

import json
import os
import secrets
import sys
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright

from .errors import CrawlerError, ErrorCode


BITBROWSER_PROVIDER = "bitbrowser"
X_PLATFORM = "x"
DOUYIN_PLATFORM = "douyin"
XIAOHONGSHU_PLATFORM = "xiaohongshu"
TELEGRAM_PLATFORM = "telegram"
SUPPORTED_SESSION_PLATFORMS = frozenset(
    {X_PLATFORM, DOUYIN_PLATFORM, XIAOHONGSHU_PLATFORM, TELEGRAM_PLATFORM}
)
_PLATFORM_PREFIX = {
    X_PLATFORM: "x",
    DOUYIN_PLATFORM: "douyin",
    XIAOHONGSHU_PLATFORM: "xhs",
    TELEGRAM_PLATFORM: "telegram",
}
_PLATFORM_LABEL = {
    X_PLATFORM: "X / Twitter",
    DOUYIN_PLATFORM: "抖音",
    XIAOHONGSHU_PLATFORM: "小红书",
    TELEGRAM_PLATFORM: "Telegram Web",
}
_PLATFORM_DOMAINS = {
    X_PLATFORM: {"x.com", "twitter.com"},
    DOUYIN_PLATFORM: {"douyin.com", "iesdouyin.com"},
    XIAOHONGSHU_PLATFORM: {"xiaohongshu.com", "xhslink.com"},
    TELEGRAM_PLATFORM: {"t.me", "telegram.me", "web.telegram.org"},
}
_AUTH_COOKIE_ALL = {X_PLATFORM: {"auth_token", "ct0"}}
_AUTH_COOKIE_ANY = {
    DOUYIN_PLATFORM: {"sessionid", "sessionid_ss", "sid_guard"},
    XIAOHONGSHU_PLATFORM: {"web_session"},
}


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    profile_id: str
    name: str


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_ref: str
    platform: str
    provider: str
    profile_id: str
    profile_name: str
    api_url: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class BrowserDownloadSession:
    """Ephemeral download material. Proxy credentials never leave this process."""

    cookiefile: Path
    proxy_url: str | None


class BitBrowserClient:
    """Small read-only client for BitBrowser's loopback local API."""

    def __init__(
        self,
        api_url: str,
        *,
        timeout_seconds: float = 8.0,
        transport: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.api_url = validate_loopback_api_url(api_url)
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def health(self) -> None:
        self._post("/health", {})

    def list_profiles(self, *, page: int = 0, page_size: int = 100) -> list[BrowserProfile]:
        data = self._post(
            "/browser/list",
            {"page": page, "pageSize": min(max(page_size, 1), 100)},
        )
        raw_profiles = data.get("list", []) if isinstance(data, dict) else data
        if not isinstance(raw_profiles, list):
            raise CrawlerError(ErrorCode.MALFORMED_RESPONSE, "比特浏览器返回了无效的 Profile 列表。")
        profiles: list[BrowserProfile] = []
        for item in raw_profiles:
            if not isinstance(item, dict):
                continue
            profile_id = str(item.get("id") or item.get("browserId") or "").strip()
            if not profile_id:
                continue
            name = str(item.get("name") or item.get("browserName") or profile_id).strip()
            profiles.append(BrowserProfile(profile_id=profile_id, name=name or profile_id))
        return profiles

    def profile_detail(self, profile_id: str) -> dict[str, Any]:
        if not profile_id.strip():
            raise CrawlerError(ErrorCode.INVALID_REQUEST, "请选择一个比特浏览器 Profile。")
        data = self._post("/browser/detail", {"id": profile_id.strip()})
        if not isinstance(data, dict):
            raise CrawlerError(ErrorCode.MALFORMED_RESPONSE, "比特浏览器返回了无效的 Profile 详情。")
        return data

    def profile_cookies(self, profile_id: str) -> list[dict[str, Any]]:
        raw = self.profile_detail(profile_id).get("cookie", [])
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "[]")
            except json.JSONDecodeError as exc:
                raise CrawlerError(ErrorCode.MALFORMED_RESPONSE, "比特浏览器 Cookie 数据格式无效。") from exc
        if not isinstance(raw, list):
            raise CrawlerError(ErrorCode.MALFORMED_RESPONSE, "比特浏览器 Cookie 数据格式无效。")
        return [item for item in raw if isinstance(item, dict)]

    def open_profile(self, profile_id: str) -> str:
        if not profile_id.strip():
            raise CrawlerError(ErrorCode.INVALID_REQUEST, "请选择一个比特浏览器 Profile。")
        profile_id = profile_id.strip()

        # Reuse an already running BitBrowser window. The official ports endpoint
        # exposes the CDP port of open profiles, so a new /browser/open call is
        # unnecessary in the common case.
        existing_endpoint = self._running_profile_endpoint(profile_id)
        if existing_endpoint:
            return existing_endpoint

        data = self._post(
            "/browser/open",
            {
                "id": profile_id,
                "loadExtensions": False,
            },
        )
        if not isinstance(data, dict):
            raise CrawlerError(ErrorCode.MALFORMED_RESPONSE, "比特浏览器返回了无效的 CDP 地址。")
        endpoint = str(data.get("ws") or "").strip()
        if not endpoint:
            http_endpoint = str(data.get("http") or "").strip()
            if http_endpoint:
                endpoint = http_endpoint if "://" in http_endpoint else f"http://{http_endpoint}"
        return validate_loopback_cdp_endpoint(endpoint)

    def _running_profile_endpoint(self, profile_id: str) -> str | None:
        """Return an active profile's local CDP endpoint without reopening it."""
        try:
            alive = self._post("/browser/pids/alive", {"ids": [profile_id]})
            if not isinstance(alive, dict) or not alive.get(profile_id):
                return None
            ports = self._post("/browser/ports", {})
        except CrawlerError:
            # Older BitBrowser clients may not expose the newer liveness API.
            # Falling back to /browser/open preserves compatibility; that API is
            # also responsible for returning a usable CDP endpoint.
            return None

        if not isinstance(ports, dict):
            return None
        raw_port: Any = ports.get(profile_id)
        if isinstance(raw_port, dict):
            raw_port = raw_port.get("port") or raw_port.get("remoteDebuggingPort")
        try:
            port = int(str(raw_port).strip())
        except (TypeError, ValueError):
            return None
        if not 1 <= port <= 65_535:
            return None
        return validate_loopback_cdp_endpoint(f"http://127.0.0.1:{port}")

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        if self._transport is not None:
            response = self._transport(path, payload)
        else:
            request = Request(
                f"{self.api_url}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=self._timeout_seconds) as stream:
                    response = json.loads(stream.read().decode("utf-8"))
            except (OSError, ValueError) as exc:
                raise CrawlerError(
                    ErrorCode.PLATFORM_UNAVAILABLE,
                    "无法连接比特浏览器本地 API。请启动比特浏览器，并检查系统设置中的 API 地址。",
                    retryable=True,
                ) from exc
        if not isinstance(response, dict):
            raise CrawlerError(ErrorCode.MALFORMED_RESPONSE, "比特浏览器本地 API 返回格式无效。")
        if response.get("success") is not True:
            message = str(response.get("msg") or "比特浏览器本地 API 请求失败。")
            raise CrawlerError(ErrorCode.PLATFORM_UNAVAILABLE, message, retryable=True)
        return response.get("data")


class SessionRegistry:
    """Maps opaque refs to BitBrowser profile IDs; no credentials or cookies are persisted."""

    def __init__(
        self,
        path: Path,
        *,
        client_factory: Callable[[str], BitBrowserClient] = BitBrowserClient,
    ) -> None:
        self.path = path.expanduser().resolve()
        self._client_factory = client_factory
        self._lock = threading.RLock()

    def register_bitbrowser(
        self,
        platform: str,
        api_url: str,
        profile_id: str,
    ) -> SessionRecord:
        platform = validate_session_platform(platform)
        client = self._client_factory(validate_loopback_api_url(api_url))
        detail = client.profile_detail(profile_id)
        _validate_profile_login(client, profile_id, platform)
        now = datetime.now(UTC).isoformat()
        record = SessionRecord(
            session_ref=f"sess_{_PLATFORM_PREFIX[platform]}_{secrets.token_urlsafe(24)}",
            platform=platform,
            provider=BITBROWSER_PROVIDER,
            profile_id=profile_id.strip(),
            profile_name=str(detail.get("name") or detail.get("browserName") or profile_id).strip(),
            api_url=client.api_url,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            records = self.list()
            records = [
                item
                for item in records
                if not (
                    item.provider == record.provider
                    and item.platform == record.platform
                    and item.api_url == record.api_url
                    and item.profile_id == record.profile_id
                )
            ]
            records.append(record)
            self._write(records)
        return record

    def register_bitbrowser_x(self, api_url: str, profile_id: str) -> SessionRecord:
        return self.register_bitbrowser(X_PLATFORM, api_url, profile_id)

    def list(self) -> list[SessionRecord]:
        with self._lock:
            if not self.path.is_file():
                return []
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                raw_records = payload.get("sessions", [])
                return [SessionRecord(**item) for item in raw_records if isinstance(item, dict)]
            except (OSError, ValueError, TypeError):
                raise CrawlerError(ErrorCode.CONFIGURATION_ERROR, "本地 session_ref 注册表已损坏。")

    def get(self, session_ref: str) -> SessionRecord:
        for record in self.list():
            if secrets.compare_digest(record.session_ref, session_ref):
                return record
        raise CrawlerError(ErrorCode.SESSION_NOT_FOUND, "session_ref 不存在或已被移除。")

    def revoke(self, session_ref: str) -> None:
        with self._lock:
            records = [item for item in self.list() if item.session_ref != session_ref]
            self._write(records)

    def validate_session(self, session_ref: str, platform: str) -> SessionRecord:
        platform = validate_session_platform(platform)
        record = self.get(session_ref)
        if record.platform != platform:
            raise CrawlerError(
                ErrorCode.INVALID_REQUEST,
                f"该 session_ref 不是 {_PLATFORM_LABEL[platform]} 会话。",
            )
        client = self._client_factory(record.api_url)
        _validate_profile_login(client, record.profile_id, platform)
        return record

    def open_browser_profile(
        self,
        session_ref: str,
        source_urls: list[str],
    ) -> tuple[SessionRecord, str, bool]:
        """Open a registered Profile without exporting browser credentials.

        This is used by browser-native extractors such as Telegram Web whose
        authenticated state lives in the Chromium profile rather than a cookie
        file.  The returned boolean only describes whether the Profile has a
        configured proxy; proxy credentials never leave this process.
        """
        record = self.get(session_ref)
        platform = validate_session_platform(record.platform)
        if not source_urls or not all(_is_platform_url(url, platform) for url in source_urls):
            raise CrawlerError(
                ErrorCode.INVALID_REQUEST,
                f"该 session_ref 仅能用于 {_PLATFORM_LABEL[platform]} 帖子地址。",
            )
        client = self._client_factory(record.api_url)
        endpoint = client.open_profile(record.profile_id)
        _validate_profile_login(client, record.profile_id, platform, cdp_endpoint=endpoint)
        detail = client.profile_detail(record.profile_id)
        return record, endpoint, _proxy_url_from_profile_detail(detail) is not None


    def validate_x_session(self, session_ref: str) -> SessionRecord:
        return self.validate_session(session_ref, X_PLATFORM)

    @contextmanager
    def materialize_cookiefile(
        self,
        session_ref: str,
        source_urls: list[str],
        working_directory: Path,
    ) -> Iterator[Path]:
        record = self.get(session_ref)
        platform = validate_session_platform(record.platform)
        if not source_urls or not all(_is_platform_url(url, platform) for url in source_urls):
            raise CrawlerError(
                ErrorCode.INVALID_REQUEST,
                f"该 session_ref 仅能用于 {_PLATFORM_LABEL[platform]} 帖子地址。",
            )
        client = self._client_factory(record.api_url)
        cookies = _profile_platform_cookies(client, record.profile_id, platform)
        cookiefile = working_directory / f".postdrop-session-{secrets.token_hex(8)}.cookies.txt"
        try:
            _write_cookiejar(cookiefile, cookies)
            yield cookiefile
        finally:
            cookiefile.unlink(missing_ok=True)

    @contextmanager
    def materialize_download_session(
        self,
        session_ref: str,
        source_urls: list[str],
        working_directory: Path,
    ) -> Iterator[BrowserDownloadSession]:
        """Materialize platform cookies and the Profile's active proxy route."""
        record = self.get(session_ref)
        platform = validate_session_platform(record.platform)
        if not source_urls or not all(_is_platform_url(url, platform) for url in source_urls):
            raise CrawlerError(
                ErrorCode.INVALID_REQUEST,
                f"该 session_ref 仅能用于 {_PLATFORM_LABEL[platform]} 帖子地址。",
            )
        client = self._client_factory(record.api_url)
        # Opening the Profile first makes BitBrowser apply its configured proxy
        # and keeps browser navigation and media download on the same route.
        client.open_profile(record.profile_id)
        detail = client.profile_detail(record.profile_id)
        cookies = _profile_platform_cookies(client, record.profile_id, platform)
        proxy_url = _proxy_url_from_profile_detail(detail)
        cookiefile = working_directory / f".postdrop-session-{secrets.token_hex(8)}.cookies.txt"
        try:
            _write_cookiejar(cookiefile, cookies)
            yield BrowserDownloadSession(cookiefile=cookiefile, proxy_url=proxy_url)
        finally:
            cookiefile.unlink(missing_ok=True)

    def _write(self, records: list[SessionRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        payload = {"version": 1, "sessions": [asdict(item) for item in records]}
        try:
            _write_private_text(
                temporary,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
            temporary.replace(self.path)
            if os.name != "nt":
                self.path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)


def default_session_registry_path() -> Path:
    configured = os.getenv("POSTDROP_SESSION_REGISTRY")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.getenv("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "PostDrop" / "sessions.json"


def validate_loopback_api_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.port is None
    ):
        raise CrawlerError(
            ErrorCode.INVALID_REQUEST,
            "比特浏览器 API 必须是带端口的本机地址，例如 http://127.0.0.1:54345。",
        )
    return candidate


def validate_session_platform(value: str) -> str:
    platform = value.strip().lower()
    if platform not in SUPPORTED_SESSION_PLATFORMS:
        raise CrawlerError(
            ErrorCode.INVALID_REQUEST,
            "登录会话平台仅支持 x、douyin、xiaohongshu、telegram。",
        )
    return platform


def validate_loopback_cdp_endpoint(value: str) -> str:
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https", "ws", "wss"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.port is None
    ):
        raise CrawlerError(
            ErrorCode.MALFORMED_RESPONSE,
            "比特浏览器返回的 CDP 地址不是有效的本机地址。",
        )
    return candidate


def _is_platform_url(value: str, platform: str) -> bool:
    host = (urlsplit(value).hostname or "").lower().rstrip(".")
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in _PLATFORM_DOMAINS[platform]
    )


def _platform_cookie_dicts(
    cookies: list[dict[str, Any]],
    platform: str,
) -> list[dict[str, Any]]:
    result = []
    for item in cookies:
        domain = str(item.get("domain") or "").lower().lstrip(".")
        if any(
            domain == allowed or domain.endswith(f".{allowed}")
            for allowed in _PLATFORM_DOMAINS[platform]
        ):
            if item.get("name") and item.get("value") is not None:
                result.append(item)
    return result


def _profile_platform_cookies(
    client: BitBrowserClient,
    profile_id: str,
    platform: str,
) -> list[dict[str, Any]]:
    """Read saved cookies, falling back to the currently open Chromium context.

    BitBrowser does not always flush a running window's new cookies into
    `/browser/detail`.  Registration and downloads must therefore consult the
    live CDP context when the saved snapshot does not prove a login.  Cookie
    values remain in memory and are never added to the session registry.
    """
    saved = _platform_cookie_dicts(client.profile_cookies(profile_id), platform)
    try:
        _require_platform_login(saved, platform)
        return saved
    except CrawlerError as saved_error:
        try:
            endpoint = client.open_profile(profile_id)
            live = _platform_cookie_dicts(_read_live_profile_cookies(endpoint), platform)
            _require_platform_login(live, platform)
            return live
        except CrawlerError as live_error:
            if live_error.code is ErrorCode.SESSION_REAUTH_REQUIRED:
                raise live_error
            raise saved_error from live_error
        except Exception as exc:
            raise saved_error from exc


def _validate_profile_login(
    client: BitBrowserClient,
    profile_id: str,
    platform: str,
    *,
    cdp_endpoint: str | None = None,
) -> None:
    if platform == TELEGRAM_PLATFORM:
        endpoint = cdp_endpoint or client.open_profile(profile_id)
        _require_telegram_web_login(endpoint)
        return
    _profile_platform_cookies(client, profile_id, platform)


def _require_telegram_web_login(cdp_endpoint: str) -> None:
    """Validate Telegram Web through its live UI, never through exported auth."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_endpoint, timeout=30_000)
        if not browser.contexts:
            raise CrawlerError(
                ErrorCode.PLATFORM_UNAVAILABLE,
                "比特浏览器窗口没有可用的浏览上下文。",
                retryable=True,
            )
        pages = [
            page
            for page in browser.contexts[0].pages
            if not page.is_closed()
            and (urlsplit(page.url).hostname or "").lower() == "web.telegram.org"
        ]
        if not pages:
            raise CrawlerError(
                ErrorCode.SESSION_REAUTH_REQUIRED,
                "该 Profile 中没有打开已登录的 Telegram Web。"
                "请先在比特浏览器中打开 web.telegram.org 并登录，再重新注册。",
            )
        page = pages[-1]
        page.wait_for_timeout(500)
        logged_in_selectors = (
            ".MessageList, .chat-list, #LeftColumn, "
            "[class*='ChatList'], [class*='MessageList']"
        )
        try:
            logged_in = page.locator(logged_in_selectors).first.is_visible(timeout=2_000)
        except Exception:
            logged_in = False
        if not logged_in:
            raise CrawlerError(
                ErrorCode.SESSION_REAUTH_REQUIRED,
                "该 Profile 中没有检测到有效的 Telegram Web 登录会话。"
                "请先在比特浏览器中手动登录 Telegram Web，再重新注册。",
            )


def _read_live_profile_cookies(cdp_endpoint: str) -> list[dict[str, Any]]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_endpoint, timeout=30_000)
        if not browser.contexts:
            raise CrawlerError(
                ErrorCode.PLATFORM_UNAVAILABLE,
                "比特浏览器窗口没有可用的浏览上下文。",
                retryable=True,
            )
        return [dict(item) for item in browser.contexts[0].cookies()]


def _cookies_from_profile_detail(detail: dict[str, Any]) -> list[dict[str, Any]]:
    raw = detail.get("cookie", [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise CrawlerError(ErrorCode.MALFORMED_RESPONSE, "比特浏览器 Cookie 数据格式无效。") from exc
    if not isinstance(raw, list):
        raise CrawlerError(ErrorCode.MALFORMED_RESPONSE, "比特浏览器 Cookie 数据格式无效。")
    return [item for item in raw if isinstance(item, dict)]


def _proxy_url_from_profile_detail(detail: dict[str, Any]) -> str | None:
    candidates = [detail]
    for key in ("proxy", "proxyInfo"):
        nested = detail.get(key)
        if isinstance(nested, dict):
            candidates.insert(0, nested)
    proxy: dict[str, Any] = {}
    for candidate in candidates:
        if candidate.get("proxyType") or candidate.get("host") or candidate.get("proxyHost"):
            proxy = candidate
            break
    raw_type = str(
        proxy.get("proxyType")
        or proxy.get("type")
        or proxy.get("proxyAgreementType")
        or ""
    ).strip().lower()
    if raw_type in {"", "noproxy", "none", "direct"}:
        return None
    scheme = {
        "http": "http",
        "https": "https",
        "socks5": "socks5",
        "socks5h": "socks5h",
        "911s5": "socks5",
    }.get(raw_type)
    if scheme is None:
        raise CrawlerError(
            ErrorCode.CONFIGURATION_ERROR,
            f"当前下载器暂不支持比特浏览器代理类型 {raw_type!r}。",
        )
    host = str(proxy.get("host") or proxy.get("proxyHost") or "").strip()
    raw_port = proxy.get("port", proxy.get("proxyPort"))
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise CrawlerError(ErrorCode.CONFIGURATION_ERROR, "比特浏览器代理端口无效。") from exc
    if not host or any(character in host for character in "/@?#") or not 1 <= port <= 65535:
        raise CrawlerError(ErrorCode.CONFIGURATION_ERROR, "比特浏览器代理地址无效。")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    username = str(
        proxy.get("proxyUserName") or proxy.get("username") or proxy.get("userName") or ""
    )
    password = str(proxy.get("proxyPassword") or proxy.get("password") or "")
    credentials = ""
    if username:
        credentials = quote(username, safe="")
        if password:
            credentials += f":{quote(password, safe='')}"
        credentials += "@"
    return f"{scheme}://{credentials}{host}:{port}"


def _require_platform_login(cookies: list[dict[str, Any]], platform: str) -> None:
    now = datetime.now(UTC).timestamp()
    names = {
        str(item.get("name"))
        for item in cookies
        if str(item.get("value") or "")
        and (
            item.get("expirationDate", item.get("expires")) in (None, "", -1)
            or _cookie_expiry_is_future(item, now)
        )
    }
    required_all = _AUTH_COOKIE_ALL.get(platform, set())
    required_any = _AUTH_COOKIE_ANY.get(platform, set())
    if not required_all.issubset(names) or (required_any and not names.intersection(required_any)):
        raise CrawlerError(
            ErrorCode.SESSION_REAUTH_REQUIRED,
            f"该 Profile 中没有检测到有效的 {_PLATFORM_LABEL[platform]} 登录会话。"
            f"请先在比特浏览器中手动登录 {_PLATFORM_LABEL[platform]}，再重新注册。",
        )


def _cookie_expiry_is_future(item: dict[str, Any], now: float) -> bool:
    try:
        return float(item.get("expirationDate", item.get("expires"))) > now
    except (TypeError, ValueError):
        return False


def _write_cookiejar(path: Path, cookies: list[dict[str, Any]]) -> None:
    jar = MozillaCookieJar(str(path))
    for item in cookies:
        domain = str(item.get("domain") or "")
        expires_value = item.get("expirationDate", item.get("expires"))
        try:
            expires = int(float(expires_value)) if expires_value not in (None, "", -1) else None
        except (TypeError, ValueError):
            expires = None
        rest: dict[str, Any] = {}
        if item.get("httpOnly"):
            rest["HttpOnly"] = True
        same_site = item.get("sameSite")
        if same_site:
            rest["SameSite"] = str(same_site)
        jar.set_cookie(
            Cookie(
                version=0,
                name=str(item["name"]),
                value=str(item.get("value") or ""),
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=bool(domain),
                domain_initial_dot=domain.startswith("."),
                path=str(item.get("path") or "/"),
                path_specified=True,
                secure=bool(item.get("secure")),
                expires=expires,
                discard=expires is None,
                comment=None,
                comment_url=None,
                rest=rest,
                rfc2109=False,
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(file_descriptor)
    jar.save(ignore_discard=True, ignore_expires=True)
    if os.name != "nt":
        path.chmod(0o600)


def _write_private_text(path: Path, value: str) -> None:
    file_descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
