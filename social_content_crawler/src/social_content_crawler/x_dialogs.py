"""Bounded X dialog handling that preserves drafts and never confirms writes."""
from __future__ import annotations

from contextlib import contextmanager

from playwright.sync_api import Locator, Page

from .diagnostics import record_exception
from .errors import CrawlerError, ErrorCode
from .x_dialog_diagnostics import PRIVATE_FIELDS, capture_dialog
from .x_dialog_rules import classify_dialog


_DIALOG = ':is([role="dialog"], [role="alertdialog"], [aria-modal="true"], [data-testid="sheetDialog"]):visible'
_COMPOSER = '[data-testid="tweetTextarea_0"], [data-testid="tweetButton"], [data-testid="tweetButtonInline"]'
_REASONS = {
    "authentication": "登录、验证或账号异常",
    "draft_confirmation": "草稿保存或丢弃确认",
    "payment_confirmation": "付费确认",
    "settings_confirmation": "设置或隐私确认",
    "interactive_form": "需要输入或选择的交互",
    "unknown": "未识别",
    "dismiss_limit": "重复出现",
}


class XDialogGuard:
    def __init__(self, page: Page, timeout_ms: float):
        self.page = page
        self.timeout_ms = timeout_ms
        self.dialog = page.locator(_DIALOG).filter(
            has_not=page.locator(f'{_DIALOG}, {_COMPOSER}'),
        ).last
        self.closed_count = 0
        self.pending_error: CrawlerError | None = None
        self._registered = False

    def _register(self) -> None:
        # Drain the stack ourselves: dynamic .last may refer to the next popup.
        self.page.add_locator_handler(self.dialog, self._handle, no_wait_after=True)
        self._registered = True

    def _remove(self) -> None:
        if self._registered and not self.page.is_closed():
            self.page.remove_locator_handler(self.dialog)
        self._registered = False

    def _handle(self) -> None:
        self._drain()

    def check(self) -> None:
        """Explicit check during readiness polling, not just clickable actions."""
        if self.pending_error:
            raise self.pending_error
        registered = self._registered
        self._remove()
        try:
            self._drain()
        finally:
            if registered and not self.page.is_closed():
                self._register()

    def _stop(self, dialog: Locator, category: str) -> None:
        directory = capture_dialog(self.page, dialog, category)
        message = f"X 出现{_REASONS.get(category, '无法安全关闭的提示')}弹框，需要人工处理，已停止本次发布。"
        if directory:
            message += f"诊断目录：{directory}"
        error = CrawlerError(
            ErrorCode.SESSION_REAUTH_REQUIRED if category == "authentication" else ErrorCode.PUBLISH_FAILED,
            message, details={"dialog_category": category, "diagnostics_dir": str(directory) if directory else None},
        )
        self.pending_error = error
        record_exception("social-content", "x_publish.dialog_requires_attention", error)
        raise error

    def _drain(self) -> None:
        if self.pending_error:
            raise self.pending_error
        while self.dialog.is_visible():
            dialog = self.dialog
            text = dialog.inner_text(timeout=self.timeout_ms)[:8000]
            buttons = dialog.get_by_role("button").all()
            labels = [(b.get_attribute("aria-label") or b.inner_text(timeout=self.timeout_ms)).strip()
                      for b in buttons if b.is_visible()]
            has_form = bool(dialog.locator(f'{PRIVATE_FIELDS}, select, [role="checkbox"], [role="switch"], iframe').count())
            decision = classify_dialog(text, labels, has_form=has_form)
            if not decision.button_name:
                self._stop(dialog, decision.category)
            if self.closed_count >= 3:
                self._stop(dialog, "dismiss_limit")
            # This handle observes removal of exactly this popup, not a new one.
            handle = dialog.element_handle(timeout=self.timeout_ms)
            try:
                dialog.get_by_role("button", name=decision.button_name, exact=True).first.click(timeout=self.timeout_ms)
                handle.wait_for_element_state("hidden", timeout=self.timeout_ms)
                self.closed_count += 1
            except Exception as exc:
                capture_dialog(self.page, dialog, "dismiss_failed")
                record_exception("social-content", "x_publish.dismiss_information_dialog", exc)
                raise
            finally:
                if handle:
                    handle.dispose()


@contextmanager
def x_information_dialogs(page: Page, *, timeout_ms: float = 2_000):
    guard = XDialogGuard(page, timeout_ms)
    guard.check()
    guard._register()
    try:
        yield guard
        if guard.pending_error:
            raise guard.pending_error
    except Exception as exc:
        if guard.pending_error and exc is not guard.pending_error:
            raise guard.pending_error from exc
        raise
    finally:
        guard._remove()
