from __future__ import annotations

import os

import pytest
from playwright.sync_api import Error, sync_playwright

from social_content_crawler.browse_backend import _extract_telegram_rows
from social_content_crawler.browse_contracts import BrowsePostsInput
from social_content_crawler.errors import CrawlerError
from social_content_crawler.telegram_dom import find_message, seek_latest


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                headless=True, executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'),
            )
        except Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip('Install Playwright Chromium to run DOM integration tests')
            raise
        page = browser.new_page()
        yield page
        browser.close()


def messages(*ids):
    return ''.join(
        f'<div class="Message message-list-item" data-message-id="{mid}" style="height:100px">'
        f'<div class="text-content">Message {mid}</div>'
        '<div class="media-inner"><canvas></canvas></div></div>' for mid in ids
    )


def mount(page, content, *, hidden=''):
    page.set_content(
        '<div class="MessageList" style="display:none">' + hidden + '</div>'
        '<div id="active" class="MessageList" style="overflow:auto;height:200px">'
        + content + '</div>'
    )


def test_find_ignores_hidden_outgoing_pane_and_scrolls_mounted_message(page):
    mount(page, messages(*range(1, 15)), hidden=messages(14))
    found = find_message(page, '14')
    assert found.evaluate("el => el.parentElement.id") == 'active'
    assert page.locator('#active').evaluate('el => el.scrollTop') > 0


def test_missing_message_stops_at_stagnant_boundary(page):
    mount(page, messages(10, 11))
    page.evaluate("""() => {
        window.deltas = [];
        document.querySelector('#active').scrollBy = (x,y) => window.deltas.push(y);
    }""")
    with pytest.raises(CrawlerError, match='连续三次滚动没有加载新消息'):
        find_message(page, '9')
    assert page.evaluate('window.deltas') == [-1200] * 3


def test_newer_missing_message_scrolls_down_not_up(page):
    mount(page, messages(10, 11))
    page.evaluate("""() => {
        window.deltas = [];
        document.querySelector('#active').scrollBy = (x,y) => window.deltas.push(y);
    }""")
    with pytest.raises(CrawlerError, match='已停止滚动'):
        find_message(page, '12')
    assert page.evaluate('window.deltas') == [1200] * 3


def test_missing_id_inside_loaded_range_does_not_scroll(page):
    mount(page, messages(10, 12))
    with pytest.raises(CrawlerError, match='目标不存在'):
        find_message(page, '11')
    assert page.locator('#active').evaluate('el => el.scrollTop') == 0


def test_find_waits_for_virtualized_target_to_mount(page):
    mount(page, messages(10, 11))
    page.evaluate("""() => {
        document.querySelector('#active').scrollBy = () => {
            const el = document.createElement('div');
            el.className = 'Message message-list-item';
            el.dataset.messageId = '9'; el.textContent = 'Target';
            document.querySelector('#active').prepend(el);
        };
    }""")
    assert find_message(page, '9').inner_text() == 'Target'


def test_seek_latest_moves_from_history_to_bottom(page):
    mount(page, messages(*range(1, 15)))
    seek_latest(page)
    state = page.locator('#active').evaluate(
        'el => el.scrollTop + el.clientHeight >= el.scrollHeight - 3'
    )
    assert state is True


def test_seek_latest_has_a_deadline(page):
    mount(page, messages(1))
    with pytest.raises(CrawlerError, match='不能将当前历史消息当作最新消息'):
        seek_latest(page, timeout_seconds=0)


def test_media_browse_sorts_ids_and_excludes_hidden_and_text_only(page):
    mount(page, messages(11, 10) +
          '<div class="Message message-list-item" data-message-id="12">Text only</div>',
          hidden=messages(999))
    request = BrowsePostsInput(
        platform='telegram', session_ref='sess_telegram_abcdefghijklmnopqrstuvwx',
        source='url', view='media', start_url='https://t.me/test_channel', max_items=1,
    )
    rows = _extract_telegram_rows(page, request)
    assert [row['url'] for row in rows] == [
        'https://t.me/test_channel/11', 'https://t.me/test_channel/10',
    ]


def add_jump_button(page, *, completes=True, hidden=False):
    page.evaluate("""({completes, hidden}) => {
        const middle = document.createElement('div'); middle.id = 'MiddleColumn';
        const button = document.createElement('button');
        button.innerHTML = '<i class="icon icon-arrow-down"></i>';
        if (hidden) middle.style.opacity = '0';
        window.jumpClicks = 0;
        button.onclick = () => {
            window.jumpClicks++;
            if (!completes) return;
            const target = document.querySelector('#active');
            target.innerHTML = '<div class="Message message-list-item" data-message-id="100">Latest</div>';
            middle.style.opacity = '0';
            middle.style.pointerEvents = 'none';
        };
        middle.append(button); document.body.append(middle);
    }""", {'completes':completes, 'hidden':hidden})


def test_latest_uses_native_jump_from_stable_history_slice(page):
    mount(page, messages(10, 11))
    add_jump_button(page)
    seek_latest(page)
    assert page.evaluate('window.jumpClicks') == 1
    assert page.locator('#active [data-message-id]').get_attribute('data-message-id') == '100'


def test_latest_rejects_stable_slice_if_native_jump_did_not_finish(page):
    mount(page, messages(10, 11))
    add_jump_button(page, completes=False)
    with pytest.raises(CrawlerError, match='不能将当前历史消息当作最新消息'):
        seek_latest(page, timeout_seconds=2)
    assert page.evaluate('window.jumpClicks') <= 2


def test_latest_does_not_click_opacity_hidden_jump_control(page):
    mount(page, messages(100))
    add_jump_button(page, hidden=True)
    seek_latest(page)
    assert page.evaluate('window.jumpClicks') == 0


def test_latest_does_not_skip_bottom_text_message(page):
    mount(page, messages(11) +
          '<div class="Message message-list-item" data-message-id="12">Newest text</div>')
    request = BrowsePostsInput(
        platform='telegram', session_ref='sess_telegram_abcdefghijklmnopqrstuvwx',
        source='url', view='latest', start_url='https://t.me/test_channel', max_items=1,
    )
    assert _extract_telegram_rows(page, request)[0]['url'] == 'https://t.me/test_channel/12'
