from __future__ import annotations

import hmac
import re
import threading
import time
from contextlib import ExitStack
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
from .diagnostics import record_exception
from .profile_tasks import GLOBAL_PROFILE_TASK_COORDINATOR, ProfileTaskCoordinator
from .sessions import BitBrowserClient, SessionRegistry
from .x_publish_contracts import XPublishInput, XPublishOutput
from .x_dialogs import x_information_dialogs


_SUPPORTED_MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov"}
_EDITORS = ('[data-testid="tweetTextarea_0"]', 'div[role="textbox"][contenteditable="true"]')
_CREATE_TWEET = re.compile(r"https://(?:[^/]+\.)?(?:x\.com|twitter\.com)/[^?#]*/CreateTweet(?:\?.*)?$")


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
        with sync_playwright() as playwright, ExitStack() as cleanup:
            try:
                browser = playwright.chromium.connect_over_cdp(
                    cdp_endpoint,
                    timeout=timeout_ms,
                )
                if not browser.contexts:
                    raise CrawlerError(ErrorCode.PUBLISH_FAILED, "比特浏览器没有可用的浏览上下文。")
                context = browser.contexts[0]
                page = _publication_page(context, cdp_endpoint)
                page.set_default_timeout(timeout_ms)
                page.goto(
                    "https://x.com/compose/post",
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                dialogs = cleanup.enter_context(x_information_dialogs(page, timeout_ms=min(timeout_ms, 2_000)))
                # X keeps its home timeline composer mounted behind the modal.
                # All controls must come from the SAME foreground composer.
                composer = _composer_scope(page, timeout_ms=min(timeout_ms, 15_000))
                editor = _first_visible(composer, _EDITORS, timeout_ms=min(timeout_ms, 10_000))
                if editor is None:
                    raise CrawlerError(
                        ErrorCode.SESSION_REAUTH_REQUIRED,
                        "未找到 X 发帖输入框，请确认该比特浏览器窗口仍处于登录状态。",
                    )
                editor.fill(request.text.strip())
                _finish_text_entry(editor, request.text, timeout_ms=min(timeout_ms, 5_000))
                if media_paths:
                    file_input = _first_attached(
                        composer,
                        (
                            'input[data-testid="fileInput"]',
                            'input[type="file"][accept*="image"]',
                            'input[type="file"][accept*="video"]',
                        ),
                    )
                    if file_input is None:
                        raise CrawlerError(ErrorCode.PUBLISH_FAILED, "X 发帖页没有可用的媒体上传控件。")
                    file_input.set_input_files([str(path) for path in media_paths])
                    _wait_for_media(composer, page, len(media_paths), timeout_ms=timeout_ms, check_dialogs=dialogs.check)

                dialogs.check()
                button = _post_button(composer, timeout_ms=min(timeout_ms, 10_000))
                _wait_until_enabled(button, page, timeout_ms=timeout_ms, check_dialogs=dialogs.check)
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


def _publication_page(context: BrowserContext, cdp_endpoint: str | None = None) -> Page:
    from .browser_lifecycle import new_task_page
    candidates = [page for page in context.pages if not page.is_closed()]
    return candidates[-1] if candidates else new_task_page(context, cdp_endpoint)


def _composer_scope(page: Page, *, timeout_ms: float) -> Page | Locator:
    dialogs = page.locator('[role="dialog"]:visible').filter(has=page.locator(', '.join(_EDITORS)))
    # Prefer the modal even when the background editor is already visible.
    try:
        dialogs.last.wait_for(state="visible", timeout=timeout_ms)
        return dialogs.last
    except PlaywrightTimeoutError:
        if page.locator('[role="dialog"]:visible').count():
            raise CrawlerError(ErrorCode.PUBLISH_FAILED, "X 页面存在其他弹框，未操作背景发帖框。")
        editors = page.locator('[data-testid="tweetTextarea_0"]:visible')
        if editors.count() != 1:
            raise CrawlerError(ErrorCode.PUBLISH_FAILED, "无法唯一确定 X 发帖框，请检查页面登录状态。")
        return page


def _first_visible(page: Page | Locator, selectors: tuple[str, ...], *, timeout_ms: float) -> Locator | None:
    # Query all alternatives together; a hidden first match must not consume a
    # whole timeout before we look at the visible foreground control.
    locator = page.locator(', '.join(f'{selector}:visible' for selector in selectors)).first
    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
        return locator
    except PlaywrightTimeoutError:
        return None


def _first_attached(page: Page | Locator, selectors: tuple[str, ...]) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count():
            return locator
    return None


def _finish_text_entry(editor: Locator, text: str, *, timeout_ms: float) -> None:
    """Commit the last hashtag/mention using the editor's normal keyboard path.

    X can leave an invisible typeahead backdrop over the submit button after
    fill(). Blur/Tab do not dismiss it, and Escape can close the whole draft.
    A real Space key terminates the token (fill(text + ' ') is NOT equivalent).
    Only trailing whitespace is added; the request guard compares stripped text.
    """
    expected = text.strip()
    if editor.inner_text(timeout=timeout_ms).strip() != expected:
        # Rich editors can append instead of replacing an existing draft. Never
        # continue with duplicated/stale text, or try to erase an unknown draft.
        raise CrawlerError(ErrorCode.PUBLISH_FAILED, "X 输入框文案与本次请求不一致，已停止发布。")
    # fill() leaves the caret at the end; End also settles the rich editor's
    # keyboard selection before sending Space. Neither key can submit a post.
    editor.press("End", timeout=timeout_ms)
    editor.press("Space", timeout=timeout_ms)
    if editor.inner_text(timeout=timeout_ms).strip() != expected:
        raise CrawlerError(ErrorCode.PUBLISH_FAILED, "X 结束标签输入后文案发生变化，已停止发布。")


def _post_button(page: Page | Locator, *, timeout_ms: float) -> Locator:
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


def _wait_for_media(composer: Page | Locator, page: Page, count: int, *, timeout_ms: float,
                    check_dialogs: Callable[[], None] | None = None) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    attachments = composer.locator('[data-testid="attachments"]')
    while time.monotonic() < deadline:
        if check_dialogs:
            check_dialogs()
        # Do not count avatars/timeline images or the selected file input itself.
        previews = attachments.locator('img[src], video').count()
        if previews >= count and not attachments.locator('[role="progressbar"]:visible').count():
            return
        page.wait_for_timeout(200)
    raise CrawlerError(ErrorCode.PUBLISH_FAILED, "X 媒体预览未就绪，已停止发布，未降级为纯文字帖子。")


def _wait_until_enabled(button: Locator, page: Page, *, timeout_ms: float,
                        check_dialogs: Callable[[], None] | None = None) -> None:
    elapsed = 0
    while elapsed < timeout_ms:
        if check_dialogs:
            check_dialogs()
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
    click_timeout = min(8_000, request.timeout_seconds * 1_000)
    # An actionability trial never dispatches a click. In particular, do not
    # force-click through a modal or spend the default 120 seconds retrying it.
    try:
        button.click(trial=True, timeout=click_timeout)
    except PlaywrightTimeoutError as exc:
        record_exception("social-content", "x_publish.click_blocked", exc)
        return XPublishOutput(state="failed", text_length=len(request.text.strip()), media_count=media_count,
            warnings=["X 发布按钮被遮挡或不可点击，未执行发布点击；请检查发帖弹框。"])

    responses = []
    blocked = []
    forwarded = False

    def guard(route):
        nonlocal forwarded
        if route.request.method != "POST":
            route.continue_()
            return
        try:
            variables = route.request.post_data_json.get("variables", {})
            entities = variables.get("media", {}).get("media_entities", [])
            matches = (variables.get("tweet_text", "").strip() == request.text.strip()
                       and isinstance(entities, list) and len(entities) == media_count
                       and all(isinstance(item, dict) and str(item.get("media_id", "")).isdigit()
                               for item in entities))
        except (AttributeError, TypeError, ValueError):
            matches = False
        if not matches or forwarded:
            blocked.append(True)
            record_exception("social-content", "x_publish.submission_mismatch",
                CrawlerError(ErrorCode.PUBLISH_FAILED, "X submission text/media count differs from requested content"))
            route.abort()
            return
        forwarded = True
        route.continue_()

    def on_response(item):
        if item.request.method == "POST" and _CREATE_TWEET.match(item.url):
            responses.append(item)

    page.route(_CREATE_TWEET, guard)
    page.on("response", on_response)
    try:
        button.click(timeout=click_timeout, no_wait_after=True)
        deadline = time.monotonic() + min(30, request.timeout_seconds)
        while not responses and not blocked and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        if not responses and not blocked:
            raise PlaywrightTimeoutError("X submission confirmation not received within bounded wait")
    except PlaywrightTimeoutError as exc:
        # A real click may have been dispatched. Never claim it definitely was,
        # never re-click, and never infer success from a pre-existing toast.
        record_exception("social-content", "x_publish.confirmation_timeout", exc)
        return XPublishOutput(state="unknown", text_length=len(request.text.strip()), media_count=media_count,
            warnings=["发布操作未获确认，无法确定是否已提交；请在 X 中核对，勿自动重试。"])
    finally:
        page.remove_listener("response", on_response)
        page.unroute(_CREATE_TWEET, guard)

    if blocked and not responses:
        return XPublishOutput(state="unknown" if forwarded else "failed",
            text_length=len(request.text.strip()), media_count=media_count,
            warnings=(["已阻止重复提交，首次提交结果尚未确认，请核对账号。"] if forwarded else
                      ["提交内容或媒体数量不匹配，已拦截发布；未降级为纯文字帖子。"]))
    response = responses[0]

    if response.status < 200 or response.status >= 300:
        record_exception("social-content", "x_publish.http_rejected",
            CrawlerError(ErrorCode.PUBLISH_FAILED, f"X returned HTTP {response.status}"))
        return XPublishOutput(
            state="failed",
            text_length=len(request.text.strip()),
            media_count=media_count,
            warnings=[f"X 返回 HTTP {response.status}；发布未确认，勿自动重试。"],
        )
    payload = _response_payload(response)
    if isinstance(payload, dict) and payload.get("errors"):
        codes = [str(item.get("code")) for item in payload["errors"][:20]
                 if isinstance(item, dict) and isinstance(item.get("code"), int)] if isinstance(payload["errors"], list) else []
        record_exception("social-content", "x_publish.graphql_rejected",
            CrawlerError(ErrorCode.PUBLISH_FAILED, f"X GraphQL rejected publication; codes={codes}"))
        return XPublishOutput(
            state="failed",
            text_length=len(request.text.strip()),
            media_count=media_count,
            warnings=["X 拒绝了发布请求；请检查账号状态、文案或媒体限制，勿自动重试。"],
        )
    post_id = _post_id_from_payload(payload)
    post_url = f"https://x.com/i/status/{post_id}" if post_id else None
    return XPublishOutput(
        state="published" if post_id else "unknown",
        post_url=post_url,
        text_length=len(request.text.strip()),
        media_count=media_count,
        warnings=[] if post_id else ["X 响应中没有可核验的帖子 ID；请核对账号，勿自动重试。"],
    )


def _response_payload(response: object) -> object:
    try:
        return response.json()  # type: ignore[attr-defined]
    except Exception as exc:
        record_exception("social-content", "x_publish.response_decode", exc)
        return None


def _post_id_from_payload(payload: object) -> str | None:
    # Never mistake the nested author's id_str or a quoted tweet ID for the
    # newly created post. Only the CreateTweet result is authoritative.
    value = payload
    for key in ("data", "create_tweet", "tweet_results", "result"):
        value = value.get(key) if isinstance(value, dict) else None
    for _ in range(4):
        if not isinstance(value, dict):
            break
        rest_id = value.get("rest_id")
        if isinstance(rest_id, str) and rest_id.isdigit():
            return rest_id
        legacy = value.get("legacy")
        legacy_id = legacy.get("id_str") if isinstance(legacy, dict) else None
        if isinstance(legacy_id, str) and legacy_id.isdigit():
            return legacy_id
        value = value.get("tweet")
    return None
