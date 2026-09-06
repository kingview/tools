"""Bounded link collection shared by anonymous and explicitly selected windows."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import time
import uuid
import re

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
from .discovery_journal import DiscoveryJournal,checkpoint_lock
from .discovery_phases import DiscoveryPhases


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


def collect(page, request, folder, *, clock=time.monotonic, review_wait_seconds=60, resume=False, resume_current_page=False,
            selected_account_url=None):
    address, model = source(request)
    state = DiscoveryState(request)
    deadline = DiscoveryDeadline(page,request.timeout_seconds,clock=clock)
    journal = DiscoveryJournal(folder)
    restored = resume and journal.load(state)

    def check_access():
        return wait_for_access(page, request, model.platform, deadline,
            validate_login=_raise_if_login_required, challenge_visible=_platform_challenge_visible,
            review_wait_seconds=review_wait_seconds)

    def read_rows():
        return (telegram_rows(page) if model is None else
            [post.model_dump(mode='json') for post in normalize_rows(model.platform, _extract_rows(page, model), 500)])

    phases = DiscoveryPhases(page, request, state, deadline, address, model, check_access, read_rows,
                             lambda address, raw: advance(page, request, address, model, raw))
    phases.confirm_account(selected_account_url)
    if state.reached_target:
        return state.result(folder)
    journal.save(state)
    try:
        deadline.check()
        if (phases.prepare_account()
                and phases.restore_position(restored=restored, current_page=resume_current_page)):
            phases.collect_pages(journal)
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


def review_page(page, request, folder, *, action):
    """Inspect the original target without collecting links or solving a challenge."""
    address, model = source(request)
    state = DiscoveryState(request)
    if not DiscoveryJournal(folder).load(state):
        raise ValueError('没有可恢复的原任务检查点，请重新执行链接发现')
    if state.selected_account_url:
        address = state.selected_account_url
    page.set_default_navigation_timeout(30000)
    if not _same_navigation_url(page.url, address):
        page.goto(address, wait_until='domcontentloaded')
    page.bring_to_front()
    if action == 'open':
        return {'needs_human_review': True, 'ready': False, 'reason': '请在此窗口完成人工处理，然后重新检查。'}
    if model is None:
        raise ValueError('Telegram 公开频道不进入登录验证流程')
    access = wait_for_access(page, request, model.platform, DiscoveryDeadline(page, 10),
        validate_login=_raise_if_login_required, challenge_visible=_platform_challenge_visible, review_wait_seconds=0)
    if access.ready and not _extract_rows(page, model):
        return {'needs_human_review': True, 'ready': False,
                'reason': '页面尚未显示可确认的帖子内容；可能仍在加载、要求登录或没有结果，请检查后再试。'}
    # Keep the owned page until the subsequent resume consumes it. No cursor,
    # links, filters or completed results are changed by this inspection.
    return {'needs_human_review': True, 'ready': access.ready,
            'reason': access.warning or ('检查通过' if access.ready else access.reason)}


def discover(request, output_root, registry=None, *, checkpoint_key=None, selected_account_url=None, review_action=None):
    from playwright.sync_api import sync_playwright
    if checkpoint_key is not None and not re.fullmatch('[a-f0-9]{32}',checkpoint_key):
        raise ValueError('无效采集检查点 ID')
    if review_action not in {None, 'open', 'check'} or (review_action and (not checkpoint_key or request.platform == 'telegram')):
        raise ValueError('无效人工检查操作')
    root=Path(output_root).resolve()/'discovered-links'
    folder=root/(checkpoint_key or uuid.uuid4().hex)
    if folder.is_symlink() or not folder.resolve().is_relative_to(Path(output_root).resolve()):
        raise ValueError('采集检查点目录越界')
    with checkpoint_lock(folder):
        if checkpoint_key is not None and request.browser_engine == 'standard':
            from .discovery_sessions import RETAINED_DISCOVERY
            from .discovery_profiles import profile_directory, persistent_page
            profile = profile_directory(checkpoint_key, DiscoveryState(request).fingerprint())

            @contextmanager
            def factory():
                with sync_playwright() as playwright:
                    with persistent_page(playwright, profile) as page:
                        yield page

            def operation(page, reused):
                if review_action:
                    return review_page(page, request, folder, action=review_action)
                result = collect(page, request, folder, resume=True, resume_current_page=reused,
                                 selected_account_url=selected_account_url)
                if result['needs_human_review']:
                    result['warnings'].append('任务专属标准窗口保留 15 分钟；浏览器配置与检查点保存在本机，重启后可继续原任务，不复用其他窗口。')
                return result

            return RETAINED_DISCOVERY.run((str(folder.resolve()), DiscoveryState(request).fingerprint()), factory, operation)
        with sync_playwright() as playwright:
            with discovery_page(playwright, request, registry) as (page, state):
                result = (review_page(page, request, folder, action=review_action) if review_action else
                          collect(page, request, folder, resume=checkpoint_key is not None,
                                  selected_account_url=selected_account_url))
                state['keep_for_review'] = result['needs_human_review']
                return result
