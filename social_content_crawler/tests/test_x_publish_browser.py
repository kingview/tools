"""Opt-in real DOM tests, isolated Chrome + loopback server; never opens X/accounts."""
import json
import os
import re
import threading
from contextlib import nullcontext
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from social_content_crawler import x_publish as xp
from social_content_crawler.x_publish_contracts import XPublishInput
from social_content_crawler.x_dialogs import x_information_dialogs

pytestmark = pytest.mark.skipif(os.getenv("SOCIAL_AGENT_BROWSER_TESTS") != "1", reason="opt-in isolated browser fixture")


@pytest.fixture
def fixture_page(monkeypatch, tmp_path):
    received=[]
    html=(Path(__file__).parent/'fixtures/x_composer.html').read_bytes()
    payload={"data":{"create_tweet":{"tweet_results":{"result":{"__typename":"Tweet","rest_id":"123456"}}}}}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header('Content-Type','text/html'); self.end_headers(); self.wfile.write(html)
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
        def log_message(self, *args):
            pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    origin=f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setattr(xp,'_CREATE_TWEET',re.compile(re.escape(origin)+r'/i/api/graphql/offline/CreateTweet$'))
    monkeypatch.setenv('SOCIAL_AGENT_LOG_DIR',str(tmp_path/'logs'))
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='chrome',headless=True)
            page=browser.new_page()
            # Prevent accidental non-loopback network access from this fixture.
            page.route('**/*',lambda route: route.continue_() if route.request.url.startswith(origin+'/') else route.abort())
            page.goto(origin)
            yield page,received,payload
            browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join()


def request():
    return XPublishInput(session_ref='sess_x_abcdefghijklmnopqrstuvwx',approval_token='test-only-approval-abcdefghijklmnopqrstuvwxyz',text='本地回归测试')


def test_foreground_controls_upload_and_single_submit(fixture_page,tmp_path):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    editor=xp._first_visible(composer,xp._EDITORS,timeout_ms=500)
    editor.fill(request().text)
    xp._finish_text_entry(editor,request().text,timeout_ms=500)
    media=tmp_path/'fixture.svg'
    media.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><rect width="4" height="4"/></svg>')
    xp._first_attached(composer,('input[data-testid="fileInput"]',)).set_input_files(str(media))
    xp._wait_for_media(composer,page,1,timeout_ms=1000)
    result=xp._submit_once(page,xp._post_button(composer,timeout_ms=500),request=request(),media_count=1)
    assert result.state=='published' and result.post_url.endswith('/123456')
    assert result.media_count==1 and len(received)==1
    assert page.evaluate('window.backgroundClicks')==0
    assert page.evaluate('window.foregroundClicks')==1
    assert page.locator('#background [contenteditable]').inner_text()==''
    assert page.locator('#background input').evaluate('(e)=>e.files.length')==0


@pytest.mark.parametrize('text',['介绍 Microduck #机器人','介绍机器人 @microduck','第一行\n第二行 #机器人'])
def test_real_space_dismisses_typeahead_without_altering_text_or_media(fixture_page,tmp_path,text):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    editor=xp._first_visible(composer,xp._EDITORS,timeout_ms=500)
    button=xp._post_button(composer,timeout_ms=500)
    page.evaluate('window.simulateTypeahead=true')
    media=tmp_path/'fixture.svg'
    media.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>')
    composer.locator('input').set_input_files([str(media)]*4)
    xp._wait_for_media(composer,page,4,timeout_ms=1000)
    editor.fill(text+' ')
    button.focus()
    with pytest.raises(xp.PlaywrightTimeoutError):
        button.click(trial=True,timeout=200)
    # Normal whitespace fill/focus was not enough. Real key completion must be.
    xp._finish_text_entry(editor,text,timeout_ms=1000)
    button.click(trial=True,timeout=1000)
    assert editor.inner_text().strip()==text
    assert composer.locator('[data-testid=attachments] img').count()==4
    assert page.evaluate('window.editorEscapes')==0
    assert page.evaluate('window.spaceKeys')==1
    assert page.evaluate('window.foregroundClicks')==0 and received==[]
    req=request().model_copy(update={'text':text})
    result=xp._submit_once(page,button,request=req,media_count=4)
    assert result.state=='published' and len(received)==1
    assert received[0]['variables']['tweet_text'].strip()==text
    assert len(received[0]['variables']['media']['media_entities'])==4
    assert page.evaluate('window.foregroundClicks')==1
    assert page.evaluate('window.backgroundClicks')==0


def test_text_mismatch_stops_before_completion_or_submit(fixture_page):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    editor=xp._first_visible(composer,xp._EDITORS,timeout_ms=500)
    editor.fill('已有草稿'+request().text)
    with pytest.raises(xp.CrawlerError,match='文案与本次请求不一致'):
        xp._finish_text_entry(editor,request().text,timeout_ms=500)
    assert received==[] and page.evaluate('window.foregroundClicks')==0
    assert page.evaluate('window.spaceKeys')==0


def test_text_completion_does_not_bypass_unrelated_overlay(fixture_page):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    editor=xp._first_visible(composer,xp._EDITORS,timeout_ms=500)
    editor.fill(request().text)
    page.locator('#blocker').evaluate('(e)=>e.style.display="block"')
    xp._finish_text_entry(editor,request().text,timeout_ms=500)
    req=request().model_copy(update={'timeout_seconds':0.25})
    result=xp._submit_once(page,xp._post_button(composer,timeout_ms=500),request=req,media_count=0)
    assert result.state=='failed' and received==[]
    assert page.evaluate('window.foregroundClicks')==0


def test_text_changed_by_completion_stops_before_submit(fixture_page):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    editor=xp._first_visible(composer,xp._EDITORS,timeout_ms=500)
    editor.fill(request().text)
    editor.evaluate('e => e.addEventListener("keyup", event => { if(event.key===" ") e.textContent+="unexpected"; })')
    with pytest.raises(xp.CrawlerError,match='结束标签输入后文案发生变化'):
        xp._finish_text_entry(editor,request().text,timeout_ms=500)
    assert received==[] and page.evaluate('window.foregroundClicks')==0


def test_request_guard_stops_silent_media_loss(fixture_page):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    xp._first_visible(composer,xp._EDITORS,timeout_ms=500).fill(request().text)
    page.evaluate('window.dropMedia=true')
    result=xp._submit_once(page,xp._post_button(composer,timeout_ms=500),request=request(),media_count=1)
    assert result.state=='failed' and received==[]
    assert page.evaluate('window.foregroundClicks')==1


def test_blocker_fails_before_real_click(fixture_page):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    page.locator('#blocker').evaluate('(e)=>e.style.display="block"')
    # Shorten only this fixture's actionability budget.
    req=request().model_copy(update={'timeout_seconds':0.25})
    result=xp._submit_once(page,xp._post_button(composer,timeout_ms=500),request=req,media_count=0)
    assert result.state=='failed' and '未执行发布点击' in result.warnings[0]
    assert received==[] and page.evaluate('window.foregroundClicks')==0


def test_missing_preview_never_reaches_submit(fixture_page):
    page,received,_=fixture_page
    with pytest.raises(xp.CrawlerError,match='媒体预览未就绪'):
        xp._wait_for_media(xp._composer_scope(page,timeout_ms=500),page,1,timeout_ms=250)
    assert received==[] and page.evaluate('window.foregroundClicks')==0


@pytest.mark.parametrize('response',[{}, {'errors':[{'code':324}]}])
def test_unverifiable_or_rejected_response_not_reported_as_published(fixture_page,response):
    page,received,payload=fixture_page
    payload.clear();payload.update(response)
    composer=xp._composer_scope(page,timeout_ms=500)
    xp._first_visible(composer,xp._EDITORS,timeout_ms=500).fill(request().text)
    result=xp._submit_once(page,xp._post_button(composer,timeout_ms=500),request=request(),media_count=0)
    assert result.state==('failed' if response else 'unknown') and len(received)==1


@pytest.mark.parametrize('title,ack,close',[
    ('可下载视频简介','知道了',True),
    ('Introducing downloadable videos','Got it',False),
    ('可下載影片簡介','知道了',False),
])
def test_initial_intro_dismissed_without_changing_draft(fixture_page,tmp_path,title,ack,close):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    editor=xp._first_visible(composer,xp._EDITORS,timeout_ms=500)
    editor.fill(request().text)
    media=tmp_path/'fixture.svg'
    media.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>')
    composer.locator('input').set_input_files(str(media))
    page.locator('#intro-title').evaluate('(e,text)=>e.textContent=text',title)
    page.locator('#intro-ack').evaluate('(e,text)=>e.textContent=text',ack)
    if not close:
        page.locator('#intro-layer [data-testid=app-bar-close]').evaluate('e=>e.remove()')
    page.evaluate('window.showIntro()')
    with x_information_dialogs(page,timeout_ms=1000):
        xp._post_button(composer,timeout_ms=500).click(trial=True,timeout=1000)
    assert page.evaluate('window.introCloses')==1
    assert editor.inner_text()==request().text
    assert composer.locator('[data-testid=attachments] img').count()==1
    assert composer.locator('input').evaluate('e=>e.files.length')==1
    assert received==[] and page.evaluate('window.foregroundClicks')==0
    assert page.evaluate('window.editorEscapes')==0


def test_late_video_intro_dismissed_before_one_submit(fixture_page,tmp_path):
    page,received,_=fixture_page
    with x_information_dialogs(page,timeout_ms=1000):
        composer=xp._composer_scope(page,timeout_ms=500)
        editor=xp._first_visible(composer,xp._EDITORS,timeout_ms=500)
        editor.fill(request().text)
        xp._finish_text_entry(editor,request().text,timeout_ms=500)
        page.evaluate('window.introOnUpload=true')
        media=tmp_path/'fixture.svg'
        media.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>')
        composer.locator('input').set_input_files(str(media))
        xp._wait_for_media(composer,page,1,timeout_ms=1000)
        result=xp._submit_once(page,xp._post_button(composer,timeout_ms=500),request=request(),media_count=1)
    assert result.state=='published'
    assert page.evaluate('window.introCloses')==1
    assert len(received)==1 and page.evaluate('window.foregroundClicks')==1
    assert len(received[0]['variables']['media']['media_entities'])==1


def test_intro_between_trial_and_real_click_does_not_repeat_submission(fixture_page):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    xp._first_visible(composer,xp._EDITORS,timeout_ms=500).fill(request().text)
    button=xp._post_button(composer,timeout_ms=500)
    class LateIntroButton:
        def click(self,**kwargs):
            button.click(**kwargs)
            if kwargs.get('trial'):
                page.evaluate('window.showIntro()')
    with x_information_dialogs(page,timeout_ms=1000):
        result=xp._submit_once(page,LateIntroButton(),request=request(),media_count=0)
    assert result.state=='published' and len(received)==1
    assert page.evaluate('window.introCloses')==1
    assert page.evaluate('window.foregroundClicks')==1


@pytest.mark.parametrize('kind',['unknown','login_form','composer'])
def test_unknown_security_or_draft_dialog_is_not_dismissed(fixture_page,kind):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    if kind=='unknown':
        page.locator('#intro-title').evaluate('e=>e.textContent="确认丢弃草稿？"')
    elif kind=='login_form':
        page.locator('#intro-title').evaluate('e=>e.parentElement.appendChild(document.createElement("input"))')
    else:
        page.locator('#intro-title').evaluate('e=>{const t=document.createElement("div");t.contentEditable=true;e.parentElement.appendChild(t)}')
    page.evaluate('window.showIntro()')
    with pytest.raises(xp.CrawlerError,match='需要人工处理'):
        with x_information_dialogs(page,timeout_ms=250):
            pytest.fail('must stop before any publish action')
    assert page.evaluate('window.introCloses')==0 and received==[]
    assert page.evaluate('window.foregroundClicks')==0


def test_information_handler_removed_after_publish_scope(fixture_page):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    with x_information_dialogs(page,timeout_ms=500):
        pass
    page.evaluate('window.showIntro()')
    with pytest.raises(xp.PlaywrightTimeoutError):
        xp._post_button(composer,timeout_ms=500).click(trial=True,timeout=250)
    assert page.evaluate('window.introCloses')==0 and received==[]


def test_unclosable_intro_logs_failure_and_preserves_draft(fixture_page,tmp_path):
    page,received,_=fixture_page
    page.evaluate('window.dismissIntro=()=>{};window.showIntro()')
    with pytest.raises(xp.PlaywrightTimeoutError):
        with x_information_dialogs(page,timeout_ms=250):
            pytest.fail('must stop before publishing')
    records=[json.loads(line) for p in (tmp_path/'logs').glob('*.jsonl') for line in p.read_text().splitlines()]
    assert records[-1]['stage']=='x_publish.dismiss_information_dialog'
    assert received==[] and page.evaluate('window.foregroundClicks')==0


def test_automation_wires_popup_handling_through_entire_publish(fixture_page,tmp_path,monkeypatch):
    page,received,_=fixture_page
    context=SimpleNamespace(pages=[page])
    browser=SimpleNamespace(contexts=[context])
    playwright=SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda *a,**kw:browser))
    monkeypatch.setattr(xp,'sync_playwright',lambda:nullcontext(playwright))
    # Reuse the isolated loopback fixture in place of navigating the real site.
    def navigate(url,**kwargs):
        assert url=='https://x.com/compose/post'
        page.evaluate('window.showIntro();window.introOnUpload=true')
    monkeypatch.setattr(page,'goto',navigate)
    media=tmp_path/'fixture.svg'
    media.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>')
    result=xp.PlaywrightXPublishAutomation().publish(cdp_endpoint='http://fixture-only',
        request=request(),media_paths=[media])
    assert result.state=='published' and result.media_count==1
    assert page.evaluate('window.introCloses')==2
    assert page.evaluate('window.foregroundClicks')==1
    assert len(received)==1
    assert received[0]['variables']['tweet_text'].strip()==request().text
    assert len(received[0]['variables']['media']['media_entities'])==1


@pytest.mark.parametrize('title,button,role',[
    ('Subscribe to Premium','No thanks','dialog'),
    ('开启通知','暂不','alertdialog'),
    ('Get the X app','Skip','dialog'),
])
def test_other_optional_prompts_choose_dismiss_not_positive_action(fixture_page,title,button,role):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    page.locator('#intro-title').evaluate('(e,t)=>e.textContent=t',title)
    page.locator('#intro-layer [role=dialog]').evaluate('(e,r)=>e.setAttribute("role",r)',role)
    page.locator('#intro-layer [data-testid=app-bar-close]').evaluate('e=>e.remove()')
    page.locator('#intro-ack').evaluate('(e,t)=>e.textContent=t',button)
    page.evaluate('window.showIntro()')
    with x_information_dialogs(page,timeout_ms=1000) as guard:
        xp._post_button(composer,timeout_ms=500).click(trial=True,timeout=1000)
        assert guard.closed_count==1
    assert page.evaluate('window.introCloses')==1 and received==[]


def test_unknown_diagnostics_are_private_cropped_and_redacted(fixture_page,tmp_path):
    page,received,_=fixture_page
    page.locator('#intro-title').evaluate('e=>e.textContent="未知弹框"')
    page.locator('#intro-ack').evaluate('''e=>{
      const input=document.createElement('input');input.value='secret-input-value';e.parentElement.append(input);
      const edit=document.createElement('div');edit.contentEditable=true;edit.textContent='secret-editor-value';e.parentElement.append(edit);
      const hidden=document.createElement('div');hidden.style.display='none';hidden.textContent='secret-hidden-value';e.parentElement.append(hidden);
      const info=document.createElement('p');info.textContent='api_key=super-secret-credential test@example.com';e.parentElement.append(info);
    }''')
    page.evaluate('window.showIntro()')
    with pytest.raises(xp.CrawlerError) as caught:
        with x_information_dialogs(page,timeout_ms=500):
            pass
    directory=Path(caught.value.details['diagnostics_dir'])
    dom=(directory/'dom.json').read_text()
    parsed=json.loads(dom)
    assert parsed['category']=='interactive_form'
    assert parsed['screenshot_fully_masked'] is True
    assert all(value not in dom for value in ['secret-input-value','secret-editor-value','secret-hidden-value',
                                              'super-secret-credential','test@example.com'])
    png=(directory/'dialog.png').read_bytes()
    assert png.startswith(b'\x89PNG')
    import struct
    width,height=struct.unpack('>II',png[16:24])
    assert width<page.viewport_size['width'] and height<page.viewport_size['height']
    assert directory.stat().st_mode & 0o777==0o700
    assert (directory/'dom.json').stat().st_mode & 0o777==0o600
    assert (directory/'dialog.png').stat().st_mode & 0o777==0o600
    assert received==[] and page.evaluate('window.introCloses')==0


def test_late_unknown_popup_stops_before_real_publish(fixture_page):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    xp._first_visible(composer,xp._EDITORS,timeout_ms=500).fill(request().text)
    button=xp._post_button(composer,timeout_ms=500)
    class LatePopupButton:
        def click(self,**kwargs):
            button.click(**kwargs)
            if kwargs.get('trial'):
                page.locator('#intro-title').evaluate('e=>e.textContent="未知弹框"')
                page.evaluate('window.showIntro()')
    with pytest.raises(xp.CrawlerError,match='需要人工处理'):
        with x_information_dialogs(page,timeout_ms=500):
            xp._submit_once(page,LatePopupButton(),request=request(),media_count=0)
    assert received==[] and page.evaluate('window.foregroundClicks')==0


def test_readiness_checks_stop_without_waiting_entire_upload_timeout(fixture_page):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    with pytest.raises(xp.CrawlerError,match='登录'):
        with x_information_dialogs(page,timeout_ms=500) as guard:
            page.locator('#intro-title').evaluate('e=>e.textContent="请登录 X"')
            page.evaluate('window.showIntro()')
            xp._wait_for_media(composer,page,1,timeout_ms=10000,check_dialogs=guard.check)
    assert received==[]


def test_repeated_optional_prompts_have_bounded_dismissal(fixture_page):
    page,received,_=fixture_page
    with pytest.raises(xp.CrawlerError,match='重复出现'):
        with x_information_dialogs(page,timeout_ms=500) as guard:
            for _ in range(4):
                page.evaluate('window.showIntro()')
                guard.check()
    assert page.evaluate('window.introCloses')==3 and received==[]


def test_diagnostic_write_failure_does_not_hide_original_blocker(fixture_page,monkeypatch):
    from social_content_crawler import x_dialog_diagnostics as diagnostics
    page,received,_=fixture_page
    monkeypatch.setattr(diagnostics,'_write_private',lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    page.locator('#intro-title').evaluate('e=>e.textContent="未知弹框"')
    page.evaluate('window.showIntro()')
    with pytest.raises(xp.CrawlerError,match='未识别'):
        with x_information_dialogs(page,timeout_ms=500):
            pass
    assert received==[]


def test_stacked_optional_dialogs_close_in_order_preserving_composer(fixture_page):
    page,received,_=fixture_page
    composer=xp._composer_scope(page,timeout_ms=500)
    page.evaluate('''() => {
      window.showIntro();
      const top=document.querySelector('#intro-layer').cloneNode(true);
      top.id='second-intro';top.style.cssText='display:block;position:fixed;inset:0;z-index:99;background:#0008';
      top.querySelector('#intro-title').textContent='开启通知';
      top.querySelectorAll('button').forEach(button=>button.onclick=()=>top.remove());
      document.body.append(top);
    }''')
    with x_information_dialogs(page,timeout_ms=1000) as guard:
        assert guard.closed_count==2
        xp._post_button(composer,timeout_ms=500).click(trial=True,timeout=1000)
    assert received==[] and composer.is_visible()


def test_sheet_dialog_without_role_is_recognized(fixture_page):
    page,received,_=fixture_page
    page.locator('#intro-layer [role=dialog]').evaluate('e=>{e.removeAttribute("role");e.dataset.testid="sheetDialog"}')
    page.evaluate('window.showIntro()')
    with x_information_dialogs(page,timeout_ms=1000) as guard:
        assert guard.closed_count==1
    assert received==[]
