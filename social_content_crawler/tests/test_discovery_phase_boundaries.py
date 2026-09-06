"""Discovery phase transitions tested without a browser or platform traffic."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from social_content_crawler import material_discovery as discovery
from social_content_crawler.discovery_contract import DiscoveryInput
from social_content_crawler.discovery_journal import DiscoveryJournal
from social_content_crawler.discovery_runtime import AccessResult
from social_content_crawler.discovery_state import DiscoveryState


class Page:
    def __init__(self, url='about:blank'):
        self.url, self.elapsed, self.gotos = url, 0, []
    def set_default_timeout(self, value): pass
    def set_default_navigation_timeout(self, value): pass
    def goto(self, url, **kwargs):
        self.url = url
        self.gotos.append(url)
    def wait_for_timeout(self, value): self.elapsed += value / 1000


def post(number):
    return {'url': f'https://www.douyin.com/video/{number}', 'media_types': ['image']}


@pytest.mark.parametrize('retained', [False, True])
def test_resume_replays_position_only_when_window_was_not_retained(tmp_path, monkeypatch, retained):
    request = DiscoveryInput(platform='douyin', source='user', user_key='id1', days=None, max_items=2)
    state = DiscoveryState(request)
    state.add([post(1)])
    state.scroll_count = 2
    DiscoveryJournal(tmp_path).save(state)
    address, _ = discovery.source(request)
    page = Page(address if retained else 'about:blank')
    access = Mock(return_value=AccessResult())
    advance = Mock(return_value=True)
    extract = Mock(return_value=[])
    monkeypatch.setattr(discovery, 'wait_for_access', access)
    monkeypatch.setattr(discovery, 'advance', advance)
    monkeypatch.setattr(discovery, '_extract_rows', extract)
    monkeypatch.setattr(discovery, 'normalize_rows', lambda *args: [SimpleNamespace(model_dump=lambda **kw: post(2))])
    result = discovery.collect(page, request, tmp_path, resume=True, resume_current_page=retained,
                               clock=lambda: page.elapsed)
    assert result['completed'] and result['count'] == 2
    assert page.gotos == ([] if retained else [address])
    assert advance.call_count == (0 if retained else 2)
    assert access.call_count == (1 if retained else 3)
    extract.assert_called_once()
    restored = DiscoveryState(request)
    assert DiscoveryJournal(tmp_path).load(restored)
    assert restored.scroll_count == 2 and restored.reached_target


def test_blocked_replay_keeps_checkpoint_and_does_not_start_collection(tmp_path, monkeypatch):
    request = DiscoveryInput(platform='douyin', source='user', user_key='id1', days=None, max_items=2)
    state = DiscoveryState(request)
    state.add([post(1)])
    state.scroll_count = 4
    DiscoveryJournal(tmp_path).save(state)
    page, extract, advance = Page(), Mock(), Mock()
    monkeypatch.setattr(discovery, 'wait_for_access', lambda *args, **kw: AccessResult(False, 'manual_review', '人工验证'))
    monkeypatch.setattr(discovery, '_extract_rows', extract)
    monkeypatch.setattr(discovery, 'advance', advance)
    result = discovery.collect(page, request, tmp_path, resume=True, clock=lambda: page.elapsed)
    assert result['needs_human_review'] and result['completion_reason'] == 'manual_review'
    assert result['count'] == 1
    extract.assert_not_called()
    advance.assert_not_called()
    restored = DiscoveryState(request)
    assert DiscoveryJournal(tmp_path).load(restored) and restored.scroll_count == 4


def test_rejected_account_selection_does_not_overwrite_checkpoint_or_navigate(tmp_path):
    request = DiscoveryInput(platform='douyin', source='user', user_key='name', account_kind='name', days=None)
    state = DiscoveryState(request)
    state.account_candidates = [{'name': 'name', 'url': 'https://www.douyin.com/user/id1'}]
    state.needs_review, state.reason = True, 'account_selection_required'
    DiscoveryJournal(tmp_path).save(state)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    page = Page()
    with pytest.raises(ValueError, match='候选'):
        discovery.collect(page, request, tmp_path, resume=True, selected_account_url='https://example.invalid/user/id1')
    assert not page.gotos
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}


def test_cached_candidates_wait_for_confirmation_without_searching_again(tmp_path, monkeypatch):
    request = DiscoveryInput(platform='douyin', source='user', user_key='name', account_kind='name', days=None)
    state = DiscoveryState(request)
    state.account_candidates = [{'name': 'name', 'url': 'https://www.douyin.com/user/id1'}]
    DiscoveryJournal(tmp_path).save(state)
    page, access, extract = Page(), Mock(), Mock()
    monkeypatch.setattr(discovery, 'wait_for_access', access)
    monkeypatch.setattr(discovery, '_extract_rows', extract)
    result = discovery.collect(page, request, tmp_path, resume=True, clock=lambda: page.elapsed)
    assert result['completion_reason'] == 'account_selection_required'
    assert result['account_candidates'] == state.account_candidates
    assert not page.gotos
    access.assert_not_called()
    extract.assert_not_called()
