from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from yt_dlp.utils import DownloadError

from social_content_crawler import backend as backend_module
from social_content_crawler import douyin_browser as db
from social_content_crawler.backend import YtDlpBackend
from social_content_crawler.contracts import DownloadInput
from social_content_crawler.errors import CrawlerError


URL = 'https://www.douyin.com/video/7679617491252907307'
ID = '7679617491252907307'
SESSION = 'sess_douyin_abcdefghijklmnopqrstuvwx'


def payload(post_id=ID):
    return {'aweme_id':post_id,'desc':'机器鸭介绍','author':{'nickname':'测试作者'},
            'video':{'duration':165000,'play_addr':{'url_list':['https://v1.douyinvod.com/clip.mp4?signature=example']}}}


def test_only_exact_post_media_is_selected_without_requiring_id_in_cdn_url():
    result=db.media_info_from_payload({'aweme_list':[payload('999'),payload()]},ID,URL)
    assert result['id']==ID and result['duration']==165
    assert result['description']=='机器鸭介绍'
    assert result['formats'][0]['url'].startswith('https://v1.douyinvod.com/clip.mp4')
    assert db.media_info_from_payload({'aweme_detail':payload('999')},ID,URL) is None


def test_camel_case_ssr_and_images():
    item={'awemeId':ID,'desc':'帖子文字','video':{'playAddr':[{'src':'https://v1.douyinvod.com/clip.mp4'}]}}
    assert db.media_info_from_payload(item,ID,URL)['formats']
    item['images']=[{'url_list':['https://p1.byteimg.com/a.jpg']},
                    {'display_image':{'url_list':['https://p2.douyinpic.com/b.webp']}}]
    result=db.media_info_from_payload(item,ID,URL)
    assert result['formats']==[] and len(result['thumbnails'])==2
    assert result['description']=='帖子文字'


@pytest.mark.parametrize('url',['http://v1.douyinvod.com/x','https://douyinvod.com.evil.com/x',
                               'https://127.0.0.1/x','https://user:secret@v1.douyinvod.com/x'])
def test_untrusted_media_urls_are_rejected(url):
    item=payload(); item['video']['play_addr']['url_list']=[url]
    assert db.media_info_from_payload(item,ID,URL) is None


def test_partial_image_data_is_not_silently_accepted():
    item=payload(); item['images']=[{'url_list':['https://p1.byteimg.com/a.jpg']},{}]
    assert db.media_info_from_payload(item,ID,URL) is None


@pytest.mark.parametrize('proxy',[None,'http://proxy-user:proxy-secret@127.0.0.1:9999'])
def test_failed_primary_reuses_cdp_and_profile_route_and_logs_error(monkeypatch,tmp_path,proxy):
    monkeypatch.setenv('SOCIAL_AGENT_LOG_DIR',str(tmp_path/'logs'))
    cookie=tmp_path/'cookies.txt'; cookie.write_text('# Netscape HTTP Cookie File\n')
    calls=[]
    class Registry:
        def get(self,_):
            return SimpleNamespace(api_url='http://127.0.0.1:54345',profile_id='douyin-test')
        def materialize_download_session(self,*args):
            return nullcontext(SimpleNamespace(cookiefile=cookie,proxy_url=proxy,cdp_endpoint='http://127.0.0.1:12345'))
    class Downloader:
        def __init__(self,options): self.options=options
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def extract_info(self,*args,**kwargs): raise DownloadError('original extractor error')
        def process_ie_result(self,info,download):
            assert self.options['cookiefile']==str(cookie)
            assert self.options.get('proxy')==proxy
            assert self.options['max_filesize']==500*1024*1024
            assert not download
            calls.append('process'); return info
        def sanitize_info(self,info): return info
    def extract(**kwargs):
        assert kwargs['cdp_endpoint']=='http://127.0.0.1:12345'
        assert kwargs['page_url']==URL
        calls.append('cdp'); return db.media_info_from_payload(payload(),ID,URL)
    monkeypatch.setattr(backend_module.yt_dlp,'YoutubeDL',Downloader)
    monkeypatch.setattr(backend_module,'extract_from_browser',extract)
    monkeypatch.setattr(backend_module,'_douyin_public_page_fallback',lambda *a: pytest.fail('anonymous fallback used'))
    backend=YtDlpBackend(session_registry=Registry())
    result=backend.run(DownloadInput(urls=[URL],session_ref=SESSION,mode='metadata_only'),tmp_path)
    assert calls==['cdp','process'] and result[0]['id']==ID
    assert backend.last_network_route==('direct' if proxy is None else 'bitbrowser_profile_proxy')
    log=''.join(p.read_text() for p in (tmp_path/'logs').glob('*.jsonl'))
    assert 'original extractor error' in log and 'download.douyin.primary' in log
    assert 'proxy-secret' not in log


def test_primary_error_survives_failed_browser_fallback(monkeypatch,tmp_path):
    monkeypatch.setenv('SOCIAL_AGENT_LOG_DIR',str(tmp_path/'logs'))
    def fail(*args,**kwargs): raise DownloadError('primary detail endpoint failed')
    def fallback(**kwargs): raise CrawlerError(backend_module.ErrorCode.DOWNLOAD_FAILED,'browser page unavailable')
    backend=YtDlpBackend()
    monkeypatch.setattr(backend,'_extract_with_browser_fallback',fail)
    monkeypatch.setattr(backend_module,'extract_from_browser',fallback)
    with pytest.raises(CrawlerError,match='browser page unavailable') as caught:
        backend._run(DownloadInput(urls=[URL],session_ref=SESSION),tmp_path,None,None,'http://127.0.0.1:12345')
    backend_module.record_exception('social-content','download.failure',caught.value)
    records=[json.loads(line) for p in (tmp_path/'logs').glob('*.jsonl') for line in p.read_text().splitlines()]
    assert records[0]['stage']=='download.douyin.primary'
    assert records[0]['exception']['message']=='primary detail endpoint failed'
    assert records[1]['exception']['message']=='browser page unavailable'
    assert records[0]['error_id']!=records[1]['error_id']


def test_mac_browser_discovery_and_missing_runtime_error(monkeypatch):
    monkeypatch.setattr(backend_module.sys,'platform','darwin')
    monkeypatch.setattr(Path,'is_file',lambda self: str(self)=='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    assert backend_module._chromium_executables()==[Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')]
    monkeypatch.setattr(backend_module,'_chromium_executables',lambda:[])
    with pytest.raises(DownloadError,match='No supported local browser found'):
        backend_module._render_public_page(URL,ID,5)


def test_browser_tab_and_connection_are_preserved(monkeypatch):
    events=[]
    page=SimpleNamespace(url=URL,is_closed=lambda:False,close=lambda:pytest.fail('existing tab closed'),
                         evaluate=lambda expression:'fixture-browser-agent')
    context=SimpleNamespace(pages=[page],new_page=lambda:pytest.fail('extra tab created'))
    browser=SimpleNamespace(contexts=[context],close=lambda:pytest.fail('browser closed'))
    chromium=SimpleNamespace(connect_over_cdp=lambda *a,**k:browser)
    monkeypatch.setattr(db,'sync_playwright',lambda:nullcontext(SimpleNamespace(chromium=chromium)))
    monkeypatch.setattr(db,'_wait_for_restored_tabs',lambda c:events.append('restore'))
    monkeypatch.setattr(db,'_read_post',lambda *a:events.append('read') or {'id':ID})
    result=db.extract_from_browser(cdp_endpoint='http://127.0.0.1:12345',page_url=URL,timeout=5)
    assert result['id']==ID and result['http_headers']=={'Referer':URL,'User-Agent':'fixture-browser-agent'}
    assert events==['restore','read']
