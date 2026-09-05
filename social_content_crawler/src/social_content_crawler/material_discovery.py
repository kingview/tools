"""Bounded link collection shared by anonymous and explicitly selected windows."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import time
import uuid

from .browse_backend import (
    _existing_platform_page, _extract_rows, _platform_challenge_visible,
    _raise_if_login_required, _same_navigation_url, _wait_for_restored_tabs,
    build_source_url, normalize_rows,
)
from .browse_contracts import BrowsePlatform, BrowseSource, BrowseView
from .profile_tasks import GLOBAL_PROFILE_TASK_COORDINATOR
from .public_materials import telegram_address, telegram_rows
from .sessions import BitBrowserClient
from .discovery_runtime import DiscoveryDeadline, BrowserTimeout, wait_for_access
from .discovery_state import DiscoveryState
from .discovery_journal import DiscoveryJournal


def source(request):
    if request.platform == 'telegram':
        channel, _ = telegram_address(request.start_url or request.user_key or '')
        return 'https://t.me/s/'+channel, None
    view = (BrowseView.LATEST if request.platform == 'xiaohongshu' and request.source == 'search'
            and request.sort == 'latest' else BrowseView.POSTS if request.source == 'user' else BrowseView.TOP)
    model = SimpleNamespace(platform=BrowsePlatform(request.platform), source=BrowseSource(request.source),
                            view=view, query=request.query, user_key=request.user_key, start_url=request.start_url)
    return build_source_url(model), model


@contextmanager
def discovery_page(playwright, request, registry=None, *, client_factory=BitBrowserClient,
                   coordinator=GLOBAL_PROFILE_TASK_COORDINATOR):
    """Close only an anonymous browser or a tab created by this call."""
    if request.browser_engine == 'standard':
        browser = playwright.chromium.launch(channel='chrome', headless=False)
        try:
            yield browser.new_page(), {'engine':'standard'}
        finally:
            browser.close()
        return
    if registry is None:
        raise ValueError('比特浏览器模式需要已注册窗口')
    record = registry.validate_session(request.session_ref, request.platform)
    with coordinator.hold(record.api_url, record.profile_id):
        client = client_factory(record.api_url)
        endpoint = client.open_profile(record.profile_id)
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=30000)
        if not browser.contexts:
            raise ValueError('比特浏览器没有可用上下文')
        context = browser.contexts[0]
        _wait_for_restored_tabs(context)
        page = _existing_platform_page(context.pages, BrowsePlatform(request.platform))
        created = page is None
        if created:
            from .browser_lifecycle import new_task_page
            page = new_task_page(context, endpoint)
        state = {'engine':'bitbrowser', 'session_ref':request.session_ref, 'keep_for_review':False}
        try:
            yield page, state
        finally:
            from .browser_lifecycle import task_manages_pages, preserve_for_review
            if state['keep_for_review']:
                preserve_for_review(endpoint)
            elif created and not task_manages_pages() and not page.is_closed():
                page.close()
            # Never browser.close(): this is the user's existing browser process.


def advance(page, request, address, model, raw):
    if model is None:
        ids = [int(row['post_id']) for row in raw if str(row.get('post_id','')).isdigit()]
        if not ids or min(ids) <= 1:
            return False
        page.goto(address+'?before='+str(min(ids)), wait_until='domcontentloaded')
    elif request.execution_mode == 'rpa':
        # Actual page input, no undocumented APIs or synthetic network requests.
        page.mouse.wheel(0, 900)
    else:
        page.evaluate('window.scrollBy(0, Math.max(600, window.innerHeight * .85))')
    return True


def collect(page, request, folder, *, clock=time.monotonic, review_wait_seconds=60):
    address, model = source(request)
    state = DiscoveryState(request)
    deadline = DiscoveryDeadline(page,request.timeout_seconds,clock=clock)
    journal = DiscoveryJournal(folder)
    journal.save(state)
    try:
        deadline.check()
        if not _same_navigation_url(page.url,address):
            page.goto(address,wait_until='domcontentloaded')
        for _ in range(500):
            deadline.check()
            if model is not None:
                access = wait_for_access(page,request,model.platform,deadline,
                    validate_login=_raise_if_login_required,challenge_visible=_platform_challenge_visible,
                    review_wait_seconds=review_wait_seconds)
                if not access.ready:
                    state.needs_review,state.reason = True,access.reason
                    state.warnings.append(access.warning)
                    break
            deadline.wait(request.access_interval_seconds)
            raw = (telegram_rows(page) if model is None else
                [post.model_dump(mode='json') for post in normalize_rows(model.platform,_extract_rows(page,model),500)])
            state.add(raw)
            if model is None:
                ids=[int(p['post_id']) for p in raw if str(p.get('post_id','')).isdigit()]
                state.cursor=str(min(ids)) if ids else None
            if state.reached_target:
                state.reason='target_reached'
            journal.save(state)
            if state.reached_target:
                break
            deadline.check()
            if state.stagnant>=3 or not advance(page,request,address,model,raw):
                break
    except BrowserTimeout:
        state.reason='timeout'
        state.warnings.append('已达到运行或页面等待时限；已发现链接已保存，可继续处理这些结果。')
    except Exception:
        state.reason='error'
        raise
    finally:
        # Unexpected browser errors still leave the last valid selection and
        # cursor on disk. No cookies, browser handles or credentials are stored.
        journal.save(state)
    return state.result(folder)


def discover(request, output_root, registry=None):
    from playwright.sync_api import sync_playwright
    folder = Path(output_root)/'discovered-links'/uuid.uuid4().hex
    with sync_playwright() as playwright:
        with discovery_page(playwright, request, registry) as (page, state):
            result = collect(page, request, folder)
            state['keep_for_review'] = result['needs_human_review']
            if result['needs_human_review'] and request.browser_engine == 'standard':
                result['warnings'].append('匿名窗口会在本次调用结束后关闭；可改选比特窗口以保留人工处理环境。')
            return result
