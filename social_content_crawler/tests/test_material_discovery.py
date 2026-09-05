from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest
from social_content_crawler import material_discovery as discovery
from social_content_crawler import browser_lifecycle
from social_content_crawler.public_materials import DiscoveryInput


def request(**options):
    return DiscoveryInput(**dict(platform='telegram',start_url='https://t.me/example_channel',days=None,max_items=2,**options))


def post(index,**changes):
    return dict(url=f'https://t.me/example_channel/{index}',post_id=str(index),
        published_at=f'2026-09-06T00:00:{index:02d}Z',media_types=['image'],**changes)


class Page:
    def __init__(self):
        self.url='about:blank'; self.elapsed=0; self.gotos=[]; self.mouse=Mock(); self.evaluate=Mock()
    def set_default_timeout(self,value): pass
    def set_default_navigation_timeout(self,value): pass
    def goto(self,url,**kwargs): self.url=url; self.gotos.append(url)
    def wait_for_timeout(self,value): self.elapsed+=value/1000
    def bring_to_front(self): pass


@pytest.mark.parametrize('options',[
    {'browser_engine':'bitbrowser'}, {'execution_mode':'rpa'},
    {'session_ref':'sess_xhs_'+'a'*24}, {'timeout_seconds':1}, {'access_interval_seconds':0},
])
def test_telegram_rejects_invalid_modes(options):
    with pytest.raises(ValueError): request(**options)


def test_explicit_matching_bit_window_required():
    for session in (None,'sess_douyin_'+'a'*24):
        with pytest.raises(ValueError):
            DiscoveryInput(platform='xiaohongshu',source='timeline',browser_engine='bitbrowser',session_ref=session)
    assert DiscoveryInput(platform='xiaohongshu',source='timeline',browser_engine='bitbrowser',session_ref='sess_xhs_'+'a'*24)
    with pytest.raises(ValueError): DiscoveryInput(platform='telegram',start_url='https://t.me/example_channel/12')


def test_latest_selected_before_quantity_cap_and_duplicates_do_not_count(tmp_path,monkeypatch):
    page=Page()
    monkeypatch.setattr(discovery,'telegram_rows',lambda p:[post(3),post(4),post(5)])
    result=discovery.collect(page,request(),tmp_path,clock=lambda:page.elapsed)
    assert [p['post_id'] for p in result['posts']]==['5','4']
    assert result['completed'] and len(page.gotos)==1
    assert json.loads((tmp_path/'metadata.json').read_text())==result['posts']


def test_filtered_duplicates_do_not_finish_target(tmp_path,monkeypatch):
    page=Page()
    rows=iter([[post(5),post(4,metrics={'views':1})],[post(5),post(3)]])
    monkeypatch.setattr(discovery,'telegram_rows',lambda _:next(rows))
    result=discovery.collect(page,request(minimum_views=2),tmp_path,clock=lambda:page.elapsed)
    assert result['completed'] and result['filtered_out']==1 and result['skipped_duplicates']==1
    assert result['found']==3 and len(page.gotos)==2


def test_navigation_timeout_preserves_partial_links(tmp_path,monkeypatch):
    page=Page()
    monkeypatch.setattr(discovery,'telegram_rows',lambda _:[post(10)])
    def timeout(*args): raise discovery.BrowserTimeout('navigation failed')
    monkeypatch.setattr(discovery,'advance',timeout)
    result=discovery.collect(page,request(),tmp_path,clock=lambda:page.elapsed)
    assert result['completion_reason']=='timeout' and not result['completed']
    assert result['count']==1 and (tmp_path/'links.txt').read_text().endswith('/10')


def test_total_deadline_prevents_another_scroll(tmp_path,monkeypatch):
    page=Page()
    monkeypatch.setattr(discovery,'telegram_rows',lambda _:[post(10)])
    advance=Mock(return_value=True); monkeypatch.setattr(discovery,'advance',advance)
    result=discovery.collect(page,request(timeout_seconds=10,access_interval_seconds=10),tmp_path,clock=lambda:page.elapsed)
    assert result['completion_reason']=='timeout' and result['count']==0
    advance.assert_not_called()


def test_rpa_and_automation_use_distinct_scroll_actions():
    page=Page()
    for mode in ('rpa','automation'):
        assert discovery.advance(page,SimpleNamespace(execution_mode=mode),'',object(),[])
    page.mouse.wheel.assert_called_once_with(0,900)
    page.evaluate.assert_called_once()


def test_manual_review_and_successful_human_intervention(tmp_path,monkeypatch):
    req=DiscoveryInput(platform='xiaohongshu',source='timeline',days=None,max_items=1)
    monkeypatch.setattr(discovery,'_raise_if_login_required',lambda *args:None)
    monkeypatch.setattr(discovery,'_platform_challenge_visible',lambda page:True)
    page=Page()
    result=discovery.collect(page,req,tmp_path,clock=lambda:page.elapsed,review_wait_seconds=1)
    assert result['needs_human_review'] and result['completion_reason']=='manual_review'
    # No automated click/solve, only checking for a human-cleared page.
    page=Page()
    monkeypatch.setattr(discovery,'_platform_challenge_visible',lambda page:page.elapsed<1)
    monkeypatch.setattr(discovery,'_extract_rows',lambda *args:[])
    monkeypatch.setattr(discovery,'normalize_rows',lambda *args:[SimpleNamespace(model_dump=lambda **kw:post(4))])
    result=discovery.collect(page,req,tmp_path,clock=lambda:page.elapsed,review_wait_seconds=2)
    assert result['completed'] and not result['needs_human_review']


@pytest.mark.parametrize('existing,review',[(True,False),(False,False),(False,True)])
def test_bit_cleanup_keeps_user_browser_and_reuses_original_tab(monkeypatch,existing,review):
    page=Mock(); page.is_closed.return_value=False
    context=SimpleNamespace(pages=[page] if existing else [])
    browser=Mock(); browser.contexts=[context]
    playwright=Mock(); playwright.chromium.connect_over_cdp.return_value=browser
    registry=Mock(); registry.validate_session.return_value=SimpleNamespace(api_url='http://127.0.0.1:54345',profile_id='p')
    client=Mock(); client.open_profile.return_value='http://127.0.0.1:9222'
    monkeypatch.setattr(discovery,'_wait_for_restored_tabs',lambda _:None)
    monkeypatch.setattr(discovery,'_existing_platform_page',lambda *args:page if existing else None)
    monkeypatch.setattr(browser_lifecycle,'new_task_page',lambda *args:page)
    monkeypatch.setattr(browser_lifecycle,'task_manages_pages',lambda:False)
    preserve=Mock(); monkeypatch.setattr(browser_lifecycle,'preserve_for_review',preserve)
    req=DiscoveryInput(platform='xiaohongshu',source='timeline',browser_engine='bitbrowser',session_ref='sess_xhs_'+'a'*24)
    with discovery.discovery_page(playwright,req,registry,client_factory=lambda _:client,
        coordinator=SimpleNamespace(hold=lambda *args:nullcontext())) as (result,state):
        assert result is page
        state['keep_for_review']=review
    browser.close.assert_not_called()
    assert page.close.call_count==int(not existing and not review)
    assert preserve.call_count==int(review)
