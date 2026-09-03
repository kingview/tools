"""Opt-in browser regressions: intercepted virtual page, no platform requests."""
import json
import os
from urllib.parse import quote

import pytest
from playwright.sync_api import sync_playwright

from social_content_crawler.douyin_browser import _read_post
from social_content_crawler.errors import CrawlerError

pytestmark=pytest.mark.skipif(os.getenv('SOCIAL_AGENT_BROWSER_TESTS')!='1',reason='opt-in isolated browser')
URL='https://www.douyin.com/video/123'
DATA={'aweme_id':'123','desc':'目标帖子的文案','video':{'play_addr':{'url_list':['https://v1.douyinvod.com/a.mp4']}}}


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='chrome',headless=True)
        page=browser.new_page()
        # Every request is intercepted; fixtures never use logged-in profiles.
        page.route('**/*',lambda r:r.abort())
        yield page
        browser.close()


@pytest.mark.parametrize('mode',['api','render','router'])
def test_target_payload_variants(page,mode):
    payload={'recommendations':[dict(DATA,aweme_id='999')],'aweme_detail':DATA}
    if mode=='api':
        html='<body><script>fetch("/aweme/v1/web/aweme/detail/")</script></body>'
    elif mode=='render':
        html='<body><script id="RENDER_DATA" type="application/json">'+quote(json.dumps(payload))+'</script></body>'
    else:
        html='<body><script>window._ROUTER_DATA='+json.dumps(payload)+'</script></body>'
    def serve(route):
        if '/aweme/' in route.request.url:
            route.fulfill(json=payload)
        elif route.request.url==URL:
            route.fulfill(body=html,content_type='text/html')
        else:
            route.abort()
    page.route('**/*',serve)
    result=_read_post(page,URL,'123',2)
    assert result['id']=='123' and result['description']=='目标帖子的文案'
    assert result['formats'][0]['url']=='https://v1.douyinvod.com/a.mp4'


def test_wrong_post_not_downloaded_and_listener_removed(page):
    html='<body><script id="RENDER_DATA" type="application/json">'+json.dumps(dict(DATA,aweme_id='999'))+'</script></body>'
    page.route('**/*',lambda r:r.fulfill(body=html,content_type='text/html'))
    with pytest.raises(CrawlerError,match='未返回目标'):
        _read_post(page,URL,'123',0.3)
    assert not page._impl_obj.listeners('response')


def test_visible_verification_stops_extraction(page):
    page.route('**/*',lambda r:r.fulfill(body='<body>请完成安全验证</body>',content_type='text/html'))
    with pytest.raises(CrawlerError,match='验证'):
        _read_post(page,URL,'123',1)
