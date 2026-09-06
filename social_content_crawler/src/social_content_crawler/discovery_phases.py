"""Account confirmation, position recovery and collection as separate phases.

Browser I/O is supplied by the entry point; phases never own browser lifetimes.
The durable state and checkpoint schema remain in DiscoveryState/DiscoveryJournal.
"""
from dataclasses import dataclass
from typing import Callable

from .browse_backend import _same_navigation_url
from .discovery_accounts import search_url
from . import discovery_accounts


@dataclass
class DiscoveryPhases:
    page: object
    request: object
    state: object
    deadline: object
    address: str
    model: object
    check_access: Callable
    read_rows: Callable
    advance: Callable

    @property
    def account_lookup(self):
        return (self.request.source == 'user' and self.request.account_kind == 'name'
                and self.request.platform != 'telegram')

    def confirm_account(self, selected_url):
        """Validate before any checkpoint write or page action, even after success."""
        if not selected_url:
            return
        if not self.account_lookup or selected_url not in {c['url'] for c in self.state.account_candidates}:
            raise ValueError('请先搜索并确认本任务的候选账号')
        if self.state.selected_account_url and self.state.selected_account_url != selected_url:
            raise ValueError('已开始采集的账号不能变更，请新建任务')
        self.state.selected_account_url = selected_url

    def require_access(self):
        if self.model is None:
            return True
        access = self.check_access()
        if not access.ready:
            self.state.needs_review, self.state.reason = True, access.reason
            self.state.warnings.append(access.warning)
        return access.ready

    def prepare_account(self):
        """Return False when human confirmation/access is needed; never guess."""
        if not self.account_lookup:
            return True
        if self.state.selected_account_url:
            self.address = self.state.selected_account_url
            return True
        if not self.state.account_candidates:
            search = search_url(self.request)
            if not _same_navigation_url(self.page.url, search):
                self.page.goto(search, wait_until='domcontentloaded')
            if not self.require_access():
                return False
            for _ in range(5):
                self.deadline.wait(self.request.access_interval_seconds)
                self.state.account_candidates = discovery_accounts.candidates(self.page, self.request.platform)
                if self.state.account_candidates:
                    break
        self.state.needs_review, self.state.reason = True, 'account_selection_required'
        self.state.warnings.append('请在任务中心点击“确认来源账号”，选择匹配的账号后继续。'
            if self.state.account_candidates else '当前页面未找到可确认的账号，请检查搜索页面或提供明确的账号主页链接。')
        return False

    def restore_position(self, *, restored, current_page):
        """Reuse an actual profile page, or replay the saved cursor/scroll count."""
        if self.account_lookup:
            current_page = current_page and _same_navigation_url(self.page.url, self.address)
        initial = self.address
        if self.model is None and restored and self.state.cursor:
            initial += '?before=' + self.state.cursor
        if not current_page and ((restored and self.model is not None)
                                 or not _same_navigation_url(self.page.url, initial)):
            self.page.goto(initial, wait_until='domcontentloaded')
        if restored and self.model is not None and not current_page:
            for _ in range(self.state.scroll_count):
                self.deadline.check()
                if not self.require_access():
                    return False
                self.advance(self.address, [])
                self.deadline.wait(self.request.access_interval_seconds)
        return True

    def collect_pages(self, journal):
        for _ in range(500):
            self.deadline.check()
            if not self.require_access():
                break
            self.deadline.wait(self.request.access_interval_seconds)
            raw = self.read_rows()
            self.state.add(raw)
            if self.model is None:
                ids = [int(p['post_id']) for p in raw if str(p.get('post_id', '')).isdigit()]
                self.state.cursor = str(min(ids)) if ids else None
            if self.state.reached_target:
                self.state.reason = 'target_reached'
            journal.save(self.state)
            if self.state.reached_target:
                break
            if self.model is not None and self.state.scroll_count >= 500:
                self.state.reason = 'page_limit'
                self.state.warnings.append('已达到本任务 500 次滚动上限，结果已保存；请缩小范围后新建任务。')
                break
            self.deadline.check()
            if self.state.stagnant >= 3 or not self.advance(self.address, raw):
                break
            if self.model is not None:
                self.state.scroll_count += 1
