"""Bounded browser waits and human intervention, separate from collection."""
import time
from dataclasses import dataclass
from playwright.sync_api import TimeoutError as BrowserTimeout
from .errors import CrawlerError
from .material_control import check_material_control


class DiscoveryDeadline:
    def __init__(self,page,seconds,*,clock=time.monotonic):
        self.page,self.clock = page,clock
        self.ends_at = clock()+seconds

    def check(self):
        check_material_control()
        remaining = self.ends_at-self.clock()
        if remaining<=0:
            raise BrowserTimeout('discovery deadline')
        milliseconds=max(1,min(30000,remaining*1000))
        self.page.set_default_timeout(milliseconds)
        self.page.set_default_navigation_timeout(milliseconds)
        return remaining

    def wait(self,seconds):
        remaining = seconds
        while remaining > 0:
            interval=min(remaining,self.check(),.3)
            self.page.wait_for_timeout(interval*1000)
            remaining-=interval
        self.check()


@dataclass(frozen=True)
class AccessResult:
    ready: bool = True
    reason: str = ''
    warning: str = ''


def wait_for_access(page,request,platform,deadline,*,validate_login,challenge_visible,review_wait_seconds):
    try:
        validate_login(page,platform)
    except CrawlerError as exc:
        return AccessResult(False,'login_required',str(exc) if request.browser_engine=='bitbrowser' else
            '标准浏览器页面要求登录；请改用已登录的比特窗口重新执行。')
    if challenge_visible(page):
        # Never solve or click through verification. The human operates the UI.
        page.bring_to_front()
        until=min(deadline.ends_at,deadline.clock()+review_wait_seconds)
        while deadline.clock()<until and challenge_visible(page):
            deadline.check()
            page.wait_for_timeout(min(500,max(1,(until-deadline.clock())*1000)))
        if challenge_visible(page):
            return AccessResult(False,'manual_review','页面需要人工验证。请在浏览器完成验证后继续，已发现链接会保留。')
    return AccessResult()
