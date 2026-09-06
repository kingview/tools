import hashlib
import json
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from social_content_crawler import material_discovery as module
from social_content_crawler import discovery_accounts,discovery_runtime
from social_content_crawler.discovery_contract import DiscoveryInput
from social_content_crawler.discovery_state import DiscoveryState
from social_content_crawler.discovery_journal import DiscoveryJournal
from social_content_crawler.discovery_profiles import profile_directory,persistent_page
from social_content_crawler.material_control import MaterialControlInterrupted


class Page:
    url='about:blank'
    def __init__(self):self.elapsed=0;self.gotos=[]
    def set_default_timeout(self,value):pass
    def set_default_navigation_timeout(self,value):pass
    def goto(self,url,**kwargs):self.url=url;self.gotos.append(url)
    def wait_for_timeout(self,value):self.elapsed+=value/1000


def test_account_name_requires_confirmation_then_resumes_same_checkpoint(tmp_path,monkeypatch):
    req=DiscoveryInput(platform='xiaohongshu',source='user',user_key='测试 名称',account_kind='name',days=None,max_items=1)
    entries=[{'name':'测试 名称','url':'https://www.xiaohongshu.com/user/profile/user1'},
             {'name':'测试 名称 · 同名','url':'https://www.xiaohongshu.com/user/profile/user2'}]
    monkeypatch.setattr(discovery_accounts,'candidates',lambda *args:entries)
    monkeypatch.setattr(module,'_raise_if_login_required',lambda *args:None)
    monkeypatch.setattr(module,'_platform_challenge_visible',lambda *args:False)
    extract=Mock(return_value=[]);monkeypatch.setattr(module,'_extract_rows',extract)
    page=Page();result=module.collect(page,req,tmp_path,resume=True,clock=lambda:page.elapsed)
    assert result['needs_human_review'] and result['account_candidates']==entries
    assert result['completion_reason']=='account_selection_required'
    extract.assert_not_called()
    assert len(page.gotos)==1 and 'search_result' in page.gotos[0]
    with pytest.raises(ValueError,match='候选'):
        module.collect(page,req,tmp_path,resume=True,selected_account_url='https://attacker.example')
    post={'url':'https://www.xiaohongshu.com/explore/post1','media_types':['image']}
    monkeypatch.setattr(module,'normalize_rows',lambda *args:[SimpleNamespace(model_dump=lambda **kw:post)])
    result=module.collect(page,req,tmp_path,resume=True,resume_current_page=True,
                          selected_account_url=entries[1]['url'],clock=lambda:page.elapsed)
    assert result['completed'] and not result['needs_human_review']
    assert page.gotos[-1]==entries[1]['url'] and result['selected_account_url']==entries[1]['url']
    with pytest.raises(ValueError,match='变更'):
        module.collect(Page(),req,tmp_path,resume=True,selected_account_url=entries[0]['url'])
    state=DiscoveryState(req);assert DiscoveryJournal(tmp_path).load(state)
    assert state.selected_account_url==entries[1]['url']


def test_id_fingerprint_remains_compatible_and_candidates_are_scoped():
    req=DiscoveryInput(platform='douyin',source='user',user_key='known-id')
    old=req.model_dump(mode='json');old.pop('account_kind')
    old_hash=hashlib.sha256(json.dumps(old,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert DiscoveryState(req).fingerprint()==old_hash
    good={'name':'name','url':'https://www.douyin.com/user/known-id?tracking=1'}
    bad=[{'name':'bad','url':url} for url in ['javascript:alert(1)','https://www.douyin.com.evil/user/id',
         'https://user:pass@www.douyin.com/user/id','https://www.douyin.com:bad/user/id',
         'https://www.douyin.com/video/id','https://www.xiaohongshu.com/user/profile/id']]
    assert discovery_accounts.normalize_candidates([good,*bad,good],'douyin')==[
        {'name':'name','url':'https://www.douyin.com/user/known-id'}]


def test_persistent_environment_reuses_private_directory_not_export_or_business_profile(tmp_path,monkeypatch):
    monkeypatch.setenv('SOCIAL_AGENT_STATE_ROOT',str(tmp_path/'state'))
    first=profile_directory('a'*32,'b'*64)
    assert first==profile_directory('a'*32,'b'*64)
    assert stat.S_IMODE(first.parent.stat().st_mode)==0o700
    with pytest.raises(ValueError,match='匹配'):profile_directory('a'*32,'c'*64)
    with pytest.raises(ValueError):profile_directory('../escape','b'*64)
    page=Mock();page.is_closed.return_value=False
    context=Mock(pages=[page]);playwright=Mock()
    playwright.chromium.launch_persistent_context.return_value=context
    with persistent_page(playwright,first) as actual:
        assert actual is page
    context.close.assert_called_once()
    playwright.chromium.launch_persistent_context.assert_called_once_with(str(first),channel='chrome',headless=False,
        args=['--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1'])
    assert stat.S_IMODE(first.stat().st_mode)==0o700
    assert first.exists()


def test_pause_inside_wait_retains_discovery_cursor_and_files(tmp_path,monkeypatch):
    page=Page();req=DiscoveryInput(platform='telegram',start_url='https://t.me/public_channel',days=None,max_items=5)
    monkeypatch.setattr(module,'telegram_rows',lambda _: [{'url':'https://t.me/public_channel/10','post_id':'10','media_types':['image']}])
    def control():
        if page.elapsed>1.2:raise MaterialControlInterrupted('paused')
    monkeypatch.setattr(discovery_runtime,'check_material_control',control)
    with pytest.raises(MaterialControlInterrupted):
        module.collect(page,req,tmp_path,resume=True,clock=lambda:page.elapsed)
    assert page.elapsed<1.6
    state=DiscoveryState(req);assert DiscoveryJournal(tmp_path).load(state)
    assert state.cursor=='10' and len(state.selected)==1
    assert (tmp_path/'links.txt').read_text()=='https://t.me/public_channel/10'
