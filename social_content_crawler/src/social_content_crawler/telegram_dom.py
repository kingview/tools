"""Bounded navigation of the active Telegram Web A message pane."""
from __future__ import annotations

from time import monotonic

from .errors import CrawlerError, ErrorCode


MESSAGE_LIST = ".MessageList:visible:not(.type-pinned)"
MESSAGES = '.Message.message-list-item[data-message-id]:visible'
BOTTOM_BUTTON = '#MiddleColumn button:has(.icon-arrow-down):visible'


def bottom_button(page):
    """The native control loads the latest slice, unlike scrollTop alone.

    Telegram keeps its floating buttons mounted with opacity/pointer-events
    hiding; Playwright's :visible alone does not exclude those controls.
    """
    controls = page.locator(BOTTOM_BUTTON)
    for index in range(controls.count()):
        control = controls.nth(index)
        shown = control.evaluate("""node => {
            for (let el = node; el; el = el.parentElement) {
                const style = getComputedStyle(el);
                if (style.visibility === 'hidden' || style.display === 'none' ||
                    Number(style.opacity) === 0 || style.pointerEvents === 'none') return false;
            }
            return !node.disabled;
        }""")
        if shown:
            return control
    return None


def message_list(page):
    # Transitions retain hidden outgoing panes. Never use the first global match.
    return page.locator(MESSAGE_LIST).last


def scroll_state(pane):
    return pane.evaluate("""node => ({
        top: Math.round(node.scrollTop), height: node.scrollHeight,
        client: node.clientHeight,
        ids: [...node.querySelectorAll('.Message.message-list-item[data-message-id]')]
          .filter(el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden')
          .map(el => Number(el.dataset.messageId)).filter(Number.isFinite)
    })""")


def seek_latest(page, *, timeout_seconds=20):
    """Use Telegram's native jump before checking the settled latest slice."""
    deadline = monotonic() + min(timeout_seconds, 20)
    previous = None
    stable = 0
    jump_attempts = 0
    for _ in range(24):
        if monotonic() >= deadline:
            break
        pane = message_list(page)
        if not pane.count():
            break
        jump = bottom_button(page)
        if jump is not None:
            # The old code could settle at the end of a virtualized history
            # slice. Telegram's own action explicitly loads the latest slice.
            if jump_attempts < 2:
                jump.click(timeout=2_000)
                jump_attempts += 1
            stable = 0
            previous = None
            page.wait_for_timeout(700)
            continue
        pane.evaluate("node => node.scrollTo(0, node.scrollHeight)")
        page.wait_for_timeout(700)
        state = scroll_state(pane)
        signature = (state['height'], tuple(state['ids']))
        at_bottom = state['top'] + state['client'] >= state['height'] - 3
        # A delayed jump button appearing after scroll means this was still a
        # history slice. Never accept it just because scrollHeight is stable.
        needs_jump = bottom_button(page) is not None
        stable = stable + 1 if at_bottom and not needs_jump and signature == previous else 0
        if stable >= 3 and state['ids']:
            return
        previous = signature
    raise CrawlerError(
        ErrorCode.BROWSE_FAILED,
        "Telegram Web 未能在限定时间内确认频道底部，不能将当前历史消息当作最新消息。",
        retryable=True,
    )


def find_message(page, message_id, *, timeout_seconds=20):
    deadline = monotonic() + min(timeout_seconds, 20)
    previous = None
    stagnant = 0
    target = int(message_id)
    reason = "定位超时"
    state = {}
    for _ in range(24):
        if monotonic() >= deadline:
            break
        pane = message_list(page)
        if not pane.count():
            reason = "没有活动消息列表"
            break
        message = pane.locator(f'.Message.message-list-item[data-message-id="{target}"]:visible').last
        if message.count():
            message.scroll_into_view_if_needed(timeout=2_000)
            return message
        state = scroll_state(pane)
        ids = state['ids']
        signature = (state['top'], state['height'], tuple(ids))
        stagnant = stagnant + 1 if signature == previous else 0
        if stagnant >= 3:
            reason = "连续三次滚动没有加载新消息"
            break
        if ids and min(ids) < target < max(ids):
            reason = "消息编号位于已加载范围内但目标不存在"
            break
        if not ids:
            # Empty/transitioning lists provide no direction information.
            page.wait_for_timeout(700)
        else:
            delta = 1_200 if target > max(ids) else -1_200
            pane.evaluate("(node, amount) => node.scrollBy(0, amount)", delta)
            page.wait_for_timeout(700)
        previous = signature
    ids = state.get('ids', [])
    bounds = f"{min(ids)}–{max(ids)}" if ids else "空"
    raise CrawlerError(
        ErrorCode.DOWNLOAD_FAILED,
        f"Telegram Web 无法定位消息 #{target}：{reason}；已加载编号范围 {bounds}。已停止滚动。",
        retryable=True,
    )
