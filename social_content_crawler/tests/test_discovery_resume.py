import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from social_content_crawler import material_discovery as module
from social_content_crawler.discovery_state import DiscoveryState
from social_content_crawler.discovery_contract import DiscoveryInput
from social_content_crawler.discovery_journal import DiscoveryJournal,checkpoint_lock


def request(**kw):return DiscoveryInput(platform='telegram',start_url='https://t.me/public_channel',days=None,max_items=3,**kw)
def post(n):return {'url':f'https://t.me/public_channel/{n}','post_id':str(n),'media_types':['image']}


class Page:
    url='about:blank'
    def __init__(self):self.elapsed=0;self.gotos=[]
    def set_default_timeout(self,value):pass
    def set_default_navigation_timeout(self,value):pass
    def goto(self,url,**kw):self.url=url;self.gotos.append(url)
    def wait_for_timeout(self,value):self.elapsed+=value/1000


def test_resume_uses_cursor_keeps_results_and_validates_request(tmp_path,monkeypatch):
    req=request(); state=DiscoveryState(req);state.add([post(9)]);state.cursor='9'
    DiscoveryJournal(tmp_path).save(state)
    monkeypatch.setattr(module,'telegram_rows',lambda p:[post(8),post(7)])
    page=Page();result=module.collect(page,req,tmp_path,resume=True,clock=lambda:page.elapsed)
    assert page.gotos==['https://t.me/s/public_channel?before=9']
    assert result['completed'] and len(result['posts'])==3
    page2=Page();assert module.collect(page2,req,tmp_path,resume=True)['completed']
    assert not page2.gotos
    with pytest.raises(ValueError,match='参数'): module.collect(Page(),request(media_type='video'),tmp_path,resume=True)


def test_checkpoint_lock_and_invalid_key(tmp_path):
    with checkpoint_lock(tmp_path):
        with pytest.raises(ValueError,match='正在运行'):
            with checkpoint_lock(tmp_path):pass
    with pytest.raises(ValueError,match='ID'):module.discover(request(),tmp_path,checkpoint_key='../invalid')


def test_corrupt_checkpoint_not_silently_replaced(tmp_path):
    path=tmp_path/'checkpoint.json';path.write_text('broken')
    with pytest.raises(ValueError):module.collect(Page(),request(),tmp_path,resume=True)
    assert path.read_text()=='broken'
