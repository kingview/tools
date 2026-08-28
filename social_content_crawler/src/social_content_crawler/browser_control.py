from __future__ import annotations

import ipaddress
import re
import threading
from collections import defaultdict
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .browser_control_contracts import (
    BrowserAction,
    BrowserInteractiveElement,
    BrowserOperationInput,
    BrowserOperationOutput,
)
from .errors import CrawlerError, ErrorCode
from .sessions import BitBrowserClient, SessionRegistry


_INTERACTIVE_SELECTOR = (
    'a[href],button,input:not([type="hidden"]),textarea,select,summary,'
    '[role="button"],[role="link"],[role="textbox"],[role="searchbox"],'
    '[role="checkbox"],[role="radio"],[role="combobox"],[role="tab"],'
    '[role="menuitem"],[contenteditable="true"]'
)
_WRITE_ACTION_LABEL = re.compile(
    r"(?:发布|发送|提交|支付|购买|下单|删除|移除|关注|取关|点赞|评论|回复|转发|私信|"
    r"publish|post|send|submit|pay|purchase|buy|delete|remove|follow|like|comment|reply|repost)",
    re.IGNORECASE,
)


class BrowserControlAutomation(Protocol):
    def perform(
        self,
        *,
        cdp_endpoint: str,
        request: BrowserOperationInput,
    ) -> BrowserOperationOutput: ...


class PlaywrightBrowserControlAutomation:
    """Operate a dedicated visible tab in an authorized BitBrowser profile."""

    def __init__(self) -> None:
        self._target_ids: dict[str, str] = {}
        self._element_refs: dict[str, dict[str, str]] = {}
        self._state_lock = threading.RLock()

    def perform(
        self,
        *,
        cdp_endpoint: str,
        request: BrowserOperationInput,
    ) -> BrowserOperationOutput:
        warnings: list[str] = []
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.connect_over_cdp(
                    cdp_endpoint,
                    timeout=request.timeout_seconds * 1_000,
                )
                if not browser.contexts:
                    raise CrawlerError(ErrorCode.BROWSE_FAILED, "比特浏览器没有可用的浏览上下文。")
                context = browser.contexts[0]
                page = self._resolve_page(context.pages, request.session_ref)
                if page is None:
                    page = context.new_page()
                page.set_default_timeout(request.timeout_seconds * 1_000)
                self._remember_page(page, request.session_ref)
                pages_before = {id(item) for item in context.pages}
                self._apply(page, request)
                if request.wait_after_ms:
                    page.wait_for_timeout(request.wait_after_ms)
                opened_pages = [
                    item
                    for item in context.pages
                    if id(item) not in pages_before and not item.is_closed()
                ]
                if opened_pages:
                    page = opened_pages[-1]
                    page.set_default_timeout(request.timeout_seconds * 1_000)
                self._remember_page(page, request.session_ref)
                elements, selectors = _snapshot_elements(page, request.max_elements)
                with self._state_lock:
                    self._element_refs[request.session_ref] = selectors
                current_url = page.url
                if not current_url.startswith("https://"):
                    warnings.append("当前标签页不是 HTTPS 页面，请勿在其中输入账号、密码或其他敏感信息。")
                return BrowserOperationOutput(
                    action=request.action,
                    url=current_url,
                    title=page.title()[:500],
                    text_excerpt=_page_text(page, request.text_excerpt_chars),
                    interactive_elements=elements,
                    warnings=warnings,
                )
            except CrawlerError:
                raise
            except PlaywrightTimeoutError as exc:
                raise CrawlerError(
                    ErrorCode.PLATFORM_UNAVAILABLE,
                    "比特浏览器页面操作超时，请检查网络、页面状态或定位条件。",
                    retryable=True,
                ) from exc
            except Exception as exc:
                raise CrawlerError(
                    ErrorCode.BROWSE_FAILED,
                    f"比特浏览器页面操作失败：{type(exc).__name__}。",
                    retryable=False,
                ) from exc

    def _apply(self, page: Page, request: BrowserOperationInput) -> None:
        action = request.action
        if action is BrowserAction.OBSERVE:
            return
        if action is BrowserAction.NAVIGATE:
            assert request.url is not None
            target_url = str(request.url)
            _require_public_https_url(target_url)
            page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=request.timeout_seconds * 1_000,
            )
            return
        if action is BrowserAction.CLICK:
            target = self._target(page, request)
            _reject_platform_write_action(target)
            target.click(timeout=request.timeout_seconds * 1_000)
            return
        if action is BrowserAction.INPUT:
            target = self._target(page, request)
            input_type = (target.get_attribute("type") or "").lower()
            if input_type in {"password", "file"}:
                raise CrawlerError(
                    ErrorCode.INVALID_REQUEST,
                    "通用浏览 Tool 不允许输入密码、验证码、密钥或上传文件。",
                )
            assert request.value is not None
            target.fill(request.value, timeout=request.timeout_seconds * 1_000)
            return
        if action is BrowserAction.PRESS:
            assert request.key is not None
            page.keyboard.press(request.key)
            return
        if action is BrowserAction.SCROLL:
            page.mouse.wheel(0, request.scroll_y)
            return
        if action is BrowserAction.BACK:
            page.go_back(wait_until="domcontentloaded", timeout=request.timeout_seconds * 1_000)
            return
        if action is BrowserAction.FORWARD:
            page.go_forward(wait_until="domcontentloaded", timeout=request.timeout_seconds * 1_000)
            return
        if action is BrowserAction.RELOAD:
            page.reload(wait_until="domcontentloaded", timeout=request.timeout_seconds * 1_000)
            return
        if action is BrowserAction.WAIT:
            page.wait_for_timeout(max(250, request.wait_after_ms or 1_000))

    def _target(self, page: Page, request: BrowserOperationInput) -> Locator:
        if request.element_ref:
            with self._state_lock:
                selector = self._element_refs.get(request.session_ref, {}).get(request.element_ref)
            if selector is None:
                raise CrawlerError(
                    ErrorCode.INVALID_REQUEST,
                    "element_ref 已失效，请先调用 observe 获取当前页面元素。",
                )
            return page.locator(selector).first
        if request.selector:
            return page.locator(request.selector).first
        if request.role:
            assert request.name is not None
            return page.get_by_role(request.role, name=request.name, exact=True).first
        assert request.text is not None
        return page.get_by_text(request.text, exact=True).first

    def _resolve_page(self, pages: list[Page], session_ref: str) -> Page | None:
        with self._state_lock:
            expected = self._target_ids.get(session_ref)
        if expected:
            for page in pages:
                if _target_id(page) == expected:
                    return page
        visible_pages = [page for page in pages if not page.is_closed() and page.url != "about:blank"]
        return visible_pages[-1] if visible_pages else None

    def _remember_page(self, page: Page, session_ref: str) -> None:
        target_id = _target_id(page)
        if target_id:
            with self._state_lock:
                self._target_ids[session_ref] = target_id


class BitBrowserControlBackend:
    def __init__(
        self,
        *,
        session_registry: SessionRegistry,
        automation: BrowserControlAutomation | None = None,
        client_factory: Callable[[str], BitBrowserClient] = BitBrowserClient,
    ) -> None:
        self._session_registry = session_registry
        self._automation = automation or PlaywrightBrowserControlAutomation()
        self._client_factory = client_factory
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

    def run(self, request: BrowserOperationInput) -> BrowserOperationOutput:
        lock = self._locks[request.session_ref]
        if not lock.acquire(timeout=5.0):
            raise CrawlerError(
                ErrorCode.SESSION_BUSY,
                "该 session_ref 正在执行另一个浏览器操作，请稍后重试。",
                retryable=True,
            )
        try:
            record = self._session_registry.get(request.session_ref)
            client = self._client_factory(record.api_url)
            cdp_endpoint = client.open_profile(record.profile_id)
            return self._automation.perform(cdp_endpoint=cdp_endpoint, request=request)
        finally:
            lock.release()


def _require_public_https_url(value: str) -> None:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise CrawlerError(ErrorCode.INVALID_REQUEST, "只能导航到不含凭据的公开 HTTPS 地址。")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise CrawlerError(ErrorCode.INVALID_REQUEST, "不能导航到本机或私有网络地址。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise CrawlerError(ErrorCode.INVALID_REQUEST, "不能导航到本机或私有网络地址。")


def _reject_platform_write_action(locator: Locator) -> None:
    attributes = [
        locator.get_attribute("aria-label") or "",
        locator.get_attribute("title") or "",
        locator.get_attribute("value") or "",
    ]
    try:
        attributes.append(locator.inner_text(timeout=2_000))
    except PlaywrightTimeoutError:
        pass
    label = " ".join(value.strip() for value in attributes if value.strip())[:1_000]
    if _WRITE_ACTION_LABEL.search(label):
        raise CrawlerError(
            ErrorCode.INVALID_REQUEST,
            "该控件可能触发发布、互动、交易或删除操作；当前 Tool 仅允许浏览、搜索和翻页。",
        )


def _target_id(page: Page) -> str | None:
    try:
        session = page.context.new_cdp_session(page)
        try:
            result = session.send("Target.getTargetInfo")
            return str(result.get("targetInfo", {}).get("targetId") or "") or None
        finally:
            session.detach()
    except Exception:
        return None


def _page_text(page: Page, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    try:
        text = page.locator("body").inner_text(timeout=3_000)
    except PlaywrightTimeoutError:
        return ""
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def _snapshot_elements(
    page: Page,
    max_elements: int,
) -> tuple[list[BrowserInteractiveElement], dict[str, str]]:
    if max_elements <= 0:
        return [], {}
    rows: list[dict[str, Any]] = page.locator(_INTERACTIVE_SELECTOR).evaluate_all(
        r"""
        (nodes, limit) => {
          const esc = (value) => CSS.escape(String(value));
          const unique = (selector) => {
            try { return document.querySelectorAll(selector).length === 1; }
            catch (_) { return false; }
          };
          const selectorFor = (node) => {
            if (node.id) {
              const candidate = `#${esc(node.id)}`;
              if (unique(candidate)) return candidate;
            }
            for (const attr of ['data-testid', 'data-e2e', 'aria-label', 'name']) {
              const value = node.getAttribute(attr);
              if (!value) continue;
              const candidate = `${node.tagName.toLowerCase()}[${attr}="${esc(value)}"]`;
              if (unique(candidate)) return candidate;
            }
            const parts = [];
            let current = node;
            while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.body) {
              const tag = current.tagName.toLowerCase();
              const siblings = current.parentElement
                ? [...current.parentElement.children].filter((item) => item.tagName === current.tagName)
                : [];
              const nth = siblings.length > 1 ? `:nth-of-type(${siblings.indexOf(current) + 1})` : '';
              parts.unshift(`${tag}${nth}`);
              const candidate = `body > ${parts.join(' > ')}`;
              if (unique(candidate)) return candidate;
              current = current.parentElement;
            }
            return `body > ${parts.join(' > ')}`;
          };
          return nodes
            .filter((node) => {
              const style = getComputedStyle(node);
              const rect = node.getBoundingClientRect();
              return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
            })
            .slice(0, limit)
            .map((node) => ({
              tag: node.tagName.toLowerCase(),
              role: node.getAttribute('role'),
              name: node.getAttribute('aria-label') || node.getAttribute('name') || null,
              text: (node.innerText || node.textContent || node.getAttribute('placeholder') || '').trim().replace(/\s+/g, ' ').slice(0, 300) || null,
              input_type: node.getAttribute('type'),
              disabled: Boolean(node.disabled || node.getAttribute('aria-disabled') === 'true'),
              selector: selectorFor(node),
            }));
        }
        """,
        max_elements,
    )
    elements: list[BrowserInteractiveElement] = []
    selectors: dict[str, str] = {}
    for index, row in enumerate(rows, start=1):
        ref = f"e{index}"
        selector = str(row.pop("selector", ""))
        if not selector:
            continue
        selectors[ref] = selector
        elements.append(BrowserInteractiveElement(ref=ref, **row))
    return elements, selectors
