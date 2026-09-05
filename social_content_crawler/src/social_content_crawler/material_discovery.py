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
from .errors import CrawlerError
from .profile_tasks import GLOBAL_PROFILE_TASK_COORDINATOR
from .public_materials import accepted, export_links, telegram_address, telegram_rows
from .sessions import BitBrowserClient
from playwright.sync_api import TimeoutError as BrowserTimeout


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
    selected, seen = [], set()
    duplicates, filtered, stagnant = 0, 0, 0
    started, warnings, reason = clock(), [], 'exhausted'
    needs_review = False

    def budget():
        remaining = request.timeout_seconds-(clock()-started)
        if remaining <= 0:
            raise BrowserTimeout('discovery deadline')
        milliseconds = max(1, min(30000, remaining*1000))
        page.set_default_timeout(milliseconds)
        page.set_default_navigation_timeout(milliseconds)
        return remaining

    def ordered(posts):
        if request.sort == 'likes':
            return sorted(posts,key=lambda p:p.get('metrics',{}).get('likes') or 0,reverse=True)
        if request.sort == 'latest':
            return sorted(posts,key=lambda p:p.get('published_at') or '',reverse=True)
        return posts

    # Even an initial navigation timeout leaves a valid empty export and a
    # structured result, rather than hiding partial output behind an exception.
    export_links([], folder)
    try:
        budget()
        if not _same_navigation_url(page.url, address):
            page.goto(address, wait_until='domcontentloaded')
        for _ in range(500):
            budget()
            if model is not None:
                try:
                    _raise_if_login_required(page, model.platform)
                except CrawlerError as exc:
                    needs_review, reason = True, 'login_required'
                    warnings.append(str(exc) if request.browser_engine == 'bitbrowser'
                                    else '标准浏览器页面要求登录；请改用已登录的比特窗口重新执行。')
                    break
                if _platform_challenge_visible(page):
                    # Never solve or dismiss verification. Keep the environment
                    # alive for bounded manual intervention and check again.
                    page.bring_to_front()
                    until = min(started+request.timeout_seconds, clock()+review_wait_seconds)
                    while clock() < until and _platform_challenge_visible(page):
                        page.wait_for_timeout(min(500,max(1,(until-clock())*1000)))
                    if _platform_challenge_visible(page):
                        needs_review, reason = True, 'manual_review'
                        warnings.append('页面需要人工验证。请在浏览器完成验证后继续，已发现链接会保留。')
                        break
            remaining = budget()
            page.wait_for_timeout(min(request.access_interval_seconds*1000,remaining*1000))
            budget()
            raw = (telegram_rows(page) if model is None else
                   [post.model_dump(mode='json') for post in normalize_rows(model.platform, _extract_rows(page, model), 500)])
            new = 0
            for post in raw:
                if post['url'] in seen:
                    duplicates += 1
                    continue
                seen.add(post['url'])
                new += 1
                if accepted(post, request):
                    selected.append(post)
                else:
                    filtered += 1
            # Order BEFORE capping: Telegram DOM rows are oldest-first, so
            # requesting the latest one must not return the first DOM row.
            export_links(ordered(selected)[:request.max_items], folder)
            if len(selected) >= request.max_items:
                reason = 'target_reached'
                break
            stagnant = 0 if new else stagnant+1
            budget()
            if stagnant >= 3 or not advance(page, request, address, model, raw):
                break
    except BrowserTimeout:
        reason = 'timeout'
        warnings.append('已达到运行或页面等待时限；已发现链接已保存，可继续处理这些结果。')
    if request.sort == 'likes':
        warnings.append('点赞排序依据本次可访问结果，不代表平台全量排名。')
    selected = ordered(selected)[:request.max_items]
    export_links(selected, folder)
    if len(selected) < request.max_items and not needs_review:
        warnings.append('符合筛选条件的可访问链接不足，已保留当前结果。未知日期不视为通过时间筛选。')
    return {'posts':selected, 'count':len(selected), 'requested':request.max_items,
            'found':len(seen), 'skipped_duplicates':duplicates, 'filtered_out':filtered,
            'completed':len(selected)==request.max_items, 'needs_human_review':needs_review,
            'completion_reason':reason, 'warnings':warnings, 'output_directory':str(folder),
            'browser_engine':request.browser_engine, 'execution_mode':request.execution_mode}


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
