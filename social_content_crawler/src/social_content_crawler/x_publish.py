from __future__ import annotations

import hmac
import re
import threading
from pathlib import Path
from typing import Callable, Protocol

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .errors import CrawlerError, ErrorCode
from .profile_tasks import GLOBAL_PROFILE_TASK_COORDINATOR, ProfileTaskCoordinator
from .sessions import BitBrowserClient, SessionRegistry
from .x_publish_contracts import XPublishInput, XPublishOutput


_SUPPORTED_MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov"}
_STATUS_ID = re.compile(r"(?:^|/)status/(\d+)(?:$|[/?#])")


class XPublishAutomation(Protocol):
    def publish(
        self,
        *,
        cdp_endpoint: str,
        request: XPublishInput,
        media_paths: list[Path],
    ) -> XPublishOutput: ...


class PlaywrightXPublishAutomation:
    """Compose exactly one X post in the visible, already logged-in profile."""

    def publish(
        self,
        *,
        cdp_endpoint: str,
        request: XPublishInput,
        media_paths: list[Path],
    ) -> XPublishOutput:
        timeout_ms = request.timeout_seconds * 1_000
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.connect_over_cdp(
                    cdp_endpoint,
                    timeout=timeout_ms,
                )
                if not browser.contexts:
                    raise CrawlerError(ErrorCode.PUBLISH_FAILED, "比特浏览器没有可用的浏览上下文。")
                context = browser.contexts[0]
                page = _publication_page(context)
                page.set_default_timeout(timeout_ms)
                page.goto(
                    "https://x.com/compose/post",
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                editor = _first_visible(
                    page,
                    (
                        '[data-testid="tweetTextarea_0"]',
                        'div[role="textbox"][contenteditable="true"]',
                    ),
                    timeout_ms=timeout_ms,
                )
                if editor is None:
                    raise CrawlerError(
                        ErrorCode.SESSION_REAUTH_REQUIRED,
                        "未找到 X 发帖输入框，请确认该比特浏览器窗口仍处于登录状态。",
                    )
                editor.fill(request.text.strip())
                if media_paths:
                    file_input = _first_attached(
                        page,
                        (
                            'input[data-testid="fileInput"]',
                            'input[type="file"][accept*="image"]',
                            'input[type="file"][accept*="video"]',
                        ),
                    )
                    if file_input is None:
                        raise CrawlerError(ErrorCode.PUBLISH_FAILED, "X 发帖页没有可用的媒体上传控件。")
                    file_input.set_input_files([str(path) for path in media_paths])

                button = _post_button(page, timeout_ms=timeout_ms)
                _wait_until_enabled(button, page, timeout_ms=timeout_ms)
                return _submit_once(
                    page,
                    button,
                    request=request,
                    media_count=len(media_paths),
                )
            except CrawlerError:
                raise
            except PlaywrightTimeoutError as exc:
                raise CrawlerError(
                    ErrorCode.PUBLISH_FAILED,
                    "X 发帖页面操作超时；如果已经点击发布，请先在 X 中确认结果，不要立即重试。",
                    retryable=False,
                ) from exc
            except Exception as exc:
                raise CrawlerError(
                    ErrorCode.PUBLISH_FAILED,
                    f"X 自动发布失败：{type(exc).__name__}。",
                    retryable=False,
                ) from exc


class XPublishBackend:
    def __init__(
        self,
        *,
        session_registry: SessionRegistry,
        output_root: Path,
        expected_approval_token: str,
        automation: XPublishAutomation | None = None,
        client_factory: Callable[[str], BitBrowserClient] = BitBrowserClient,
        task_coordinator: ProfileTaskCoordinator = GLOBAL_PROFILE_TASK_COORDINATOR,
    ) -> None:
        self._session_registry = session_registry
        self._output_root = output_root.expanduser().resolve()
        self._expected_approval_token = expected_approval_token
        self._automation = automation or PlaywrightXPublishAutomation()
        self._client_factory = client_factory
        self._task_coordinator = task_coordinator
        self._lock = threading.Lock()
        self._consumed = False

    def run(self, request: XPublishInput) -> XPublishOutput:
        if not self._expected_approval_token or not hmac.compare_digest(
            request.approval_token,
            self._expected_approval_token,
        ):
            raise CrawlerError(
                ErrorCode.APPROVAL_REQUIRED,
                "X 发布授权无效或已经过期，请重新生成并确认执行计划。",
            )
        if not self._lock.acquire(timeout=5.0):
            raise CrawlerError(ErrorCode.SESSION_BUSY, "X 发布任务正在执行，请勿重复提交。")
        try:
            if self._consumed:
                raise CrawlerError(
                    ErrorCode.APPROVAL_REQUIRED,
                    "本次 X 发布授权已经使用，不能重复发布。",
                )
            record = self._session_registry.validate_x_session(request.session_ref)
            media_paths = _validated_media_paths(request.media_paths, self._output_root)
            with self._task_coordinator.hold(record.api_url, record.profile_id):
                # Consume immediately before opening/operating the browser. An ambiguous
                # network result must never be retried with the same approval.
                self._consumed = True
                cdp_endpoint = self._client_factory(record.api_url).open_profile(record.profile_id)
                return self._automation.publish(
                    cdp_endpoint=cdp_endpoint,
                    request=request,
                    media_paths=media_paths,
                )
        finally:
            self._lock.release()


def _validated_media_paths(values: list[str], output_root: Path) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        path = Path(value).expanduser().resolve(strict=True)
        try:
            path.relative_to(output_root)
        except ValueError as exc:
            raise CrawlerError(
                ErrorCode.INVALID_REQUEST,
                "发布媒体必须来自 Social Agent 输出目录。",
            ) from exc
        if not path.is_file() or path.suffix.lower() not in _SUPPORTED_MEDIA_SUFFIXES:
            raise CrawlerError(
                ErrorCode.INVALID_REQUEST,
                f"X 不支持该发布媒体格式：{path.name}",
            )
        paths.append(path)
    return paths


def _publication_page(context: BrowserContext) -> Page:
    candidates = [page for page in context.pages if not page.is_closed()]
    return candidates[-1] if candidates else context.new_page()


def _first_visible(page: Page, selectors: tuple[str, ...], *, timeout_ms: float) -> Locator | None:
    per_selector = max(1_000, int(timeout_ms / max(len(selectors), 1)))
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=per_selector)
            return locator
        except PlaywrightTimeoutError:
            continue
    return None


def _first_attached(page: Page, selectors: tuple[str, ...]) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count():
            return locator
    return None


def _post_button(page: Page, *, timeout_ms: float) -> Locator:
    button = _first_visible(
        page,
        (
            '[data-testid="tweetButton"]',
            '[data-testid="tweetButtonInline"]',
            'button:has-text("发布")',
            'button:has-text("发帖")',
            'button:has-text("Post")',
        ),
        timeout_ms=timeout_ms,
    )
    if button is None:
        raise CrawlerError(ErrorCode.PUBLISH_FAILED, "未找到 X 发布按钮。")
    return button


def _wait_until_enabled(button: Locator, page: Page, *, timeout_ms: float) -> None:
    elapsed = 0
    while elapsed < timeout_ms:
        if button.is_enabled() and button.get_attribute("aria-disabled") != "true":
            return
        page.wait_for_timeout(500)
        elapsed += 500
    raise CrawlerError(ErrorCode.PUBLISH_FAILED, "X 发布按钮一直不可用，请检查文案或媒体处理状态。")


def _submit_once(
    page: Page,
    button: Locator,
    *,
    request: XPublishInput,
    media_count: int,
) -> XPublishOutput:
    response = None
    try:
        with page.expect_response(
            lambda item: item.request.method == "POST" and "CreateTweet" in item.url,
            timeout=min(30_000, request.timeout_seconds * 1_000),
        ) as response_info:
            button.click()
        response = response_info.value
    except PlaywrightTimeoutError:
        # The click has already happened. Report an ambiguous result and never retry.
        confirmed_url = _ui_confirmed_post_url(page)
        if confirmed_url is not None:
            return XPublishOutput(
                state="published",
                post_url=confirmed_url or None,
                text_length=len(request.text.strip()),
                media_count=media_count,
                warnings=([] if confirmed_url else ["X 页面已确认发布，但没有返回帖子地址。"]),
            )
        return XPublishOutput(
            state="unknown",
            text_length=len(request.text.strip()),
            media_count=media_count,
            warnings=["已点击发布，但未捕获到 X 的确认响应；请在账号页面核对，勿自动重试。"],
        )

    if response.status < 200 or response.status >= 300:
        return XPublishOutput(
            state="failed",
            text_length=len(request.text.strip()),
            media_count=media_count,
            warnings=[f"X 返回 HTTP {response.status}；发布未确认，勿自动重试。"],
        )
    payload = _response_payload(response)
    if isinstance(payload, dict) and payload.get("errors"):
        return XPublishOutput(
            state="failed",
            text_length=len(request.text.strip()),
            media_count=media_count,
            warnings=["X 拒绝了发布请求；请检查账号状态、文案或媒体限制，勿自动重试。"],
        )
    post_id = _post_id_from_payload(payload)
    post_url = f"https://x.com/i/status/{post_id}" if post_id else None
    return XPublishOutput(
        state="published",
        post_url=post_url,
        text_length=len(request.text.strip()),
        media_count=media_count,
        warnings=[] if post_id else ["X 已确认发布，但响应中没有可解析的帖子地址。"],
    )


def _response_payload(response: object) -> object:
    try:
        return response.json()  # type: ignore[attr-defined]
    except Exception:
        return None


def _post_id_from_payload(payload: object) -> str | None:
    stack = [payload]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            rest_id = value.get("rest_id")
            typename = str(value.get("__typename") or "")
            if isinstance(rest_id, str) and rest_id.isdigit() and "Tweet" in typename:
                return rest_id
            for key, child in value.items():
                if key in {"tweet_id", "id_str"} and isinstance(child, str) and child.isdigit():
                    return child
                stack.append(child)
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, str):
            match = _STATUS_ID.search(value)
            if match:
                return match.group(1)
    return None


def _ui_confirmed_post_url(page: Page) -> str | None:
    match = _STATUS_ID.search(page.url)
    if match:
        return f"https://x.com/i/status/{match.group(1)}"
    toast = page.locator('[data-testid="toast"]').last
    try:
        text = toast.inner_text(timeout=2_000).lower()
    except Exception:
        return None
    success_markers = ("your post was sent", "post sent", "帖子已发送", "已发布", "查看")
    return "" if any(marker in text for marker in success_markers) else None
