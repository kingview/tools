from pathlib import Path
import json

import pytest
from social_content_crawler.discovery_contract import DiscoveryInput
from social_content_crawler.discovery_state import DiscoveryState
from social_content_crawler.discovery_journal import DiscoveryJournal,atomic_text
from social_content_crawler import material_discovery as discovery


def request():
    return DiscoveryInput(platform='telegram',start_url='https://t.me/public_channel',days=None,max_items=2)


def post(index,published):
    return dict(url=f'https://t.me/public_channel/{index}',post_id=str(index),published_at=published,media_types=['image'])


def test_state_sorts_instants_not_iso_strings_and_deduplicates():
    state=DiscoveryState(request())
    older=post(1,'2026-09-06T09:00:00+08:00')
    newer=post(2,'2026-09-06T02:00:00Z')
    state.add([older,newer,older])
    assert [p['post_id'] for p in state.ordered()]==['2','1']
    assert state.duplicates==1 and len(state.seen)==2 and state.reached_target
    state.add([older]); assert state.stagnant==1


def test_checkpoint_and_exports_match_without_runtime_handles(tmp_path):
    state=DiscoveryState(request())
    state.add([post(3,'2026-09-06T00:00:00Z')]); state.cursor='3'
    state.reason='manual_review'; state.needs_review=True
    DiscoveryJournal(tmp_path).save(state)
    checkpoint=json.loads((tmp_path/'checkpoint.json').read_text())
    assert checkpoint['posts']==json.loads((tmp_path/'metadata.json').read_text())
    assert checkpoint['cursor']=='3' and checkpoint['needs_human_review']
    assert (tmp_path/'links.txt').read_text().endswith('/3')
    assert not set(checkpoint).intersection({'cookie','session_ref','page','browser','authorization'})
    assert not list(tmp_path.glob('*.tmp'))


def test_atomic_export_failure_keeps_previous_file(tmp_path,monkeypatch):
    path=tmp_path/'metadata.json'; path.write_text('previous')
    def broken_replace(*args): raise OSError('fixture full disk')
    monkeypatch.setattr(Path,'replace',broken_replace)
    with pytest.raises(OSError): atomic_text(path,'new')
    assert path.read_text()=='previous' and not list(tmp_path.glob('*.tmp'))


def test_unexpected_browser_error_still_saves_checkpoint(tmp_path,monkeypatch):
    class Page:
        url='about:blank'
        def set_default_timeout(self,*args): pass
        def set_default_navigation_timeout(self,*args): pass
        def goto(self,*args,**kwargs): pass
        def wait_for_timeout(self,*args): pass
    monkeypatch.setattr(discovery,'telegram_rows',lambda _: [post(7,'2026-09-06T00:00:00Z')])
    def broken(*args): raise RuntimeError('browser disconnected')
    monkeypatch.setattr(discovery,'advance',broken)
    with pytest.raises(RuntimeError,match='disconnected'): discovery.collect(Page(),request(),tmp_path)
    checkpoint=json.loads((tmp_path/'checkpoint.json').read_text())
    assert checkpoint['reason']=='error' and checkpoint['posts'][0]['post_id']=='7'
