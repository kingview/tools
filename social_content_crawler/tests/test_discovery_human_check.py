import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from social_content_crawler import material_discovery as module, public_materials, discovery_profiles
from social_content_crawler.discovery_contract import DiscoveryInput
from social_content_crawler.discovery_state import DiscoveryState
from social_content_crawler.discovery_journal import DiscoveryJournal


@pytest.mark.parametrize('action', ['open', 'check'])
def test_compatibility_entry_forwards_account_and_human_action(tmp_path, monkeypatch, action):
    run = Mock(return_value={'ready': False}); monkeypatch.setattr(module, 'discover', run)
    request = DiscoveryInput(platform='xiaohongshu', source='timeline')
    public_materials.discover(request, tmp_path, checkpoint_key='a'*32, selected_account_url='chosen', review_action=action)
    assert run.call_args.kwargs == {'checkpoint_key': 'a'*32, 'selected_account_url': 'chosen', 'review_action': action}


@pytest.mark.parametrize('action,challenge,posts,ready', [('open', False, True, False),
    ('check', True, True, False), ('check', False, False, False), ('check', False, True, True)])
def test_human_inspection_never_scrolls_or_changes_checkpoint(tmp_path, monkeypatch, action, challenge, posts, ready):
    request = DiscoveryInput(platform='xiaohongshu', source='timeline')
    state = DiscoveryState(request); DiscoveryJournal(tmp_path).save(state)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    address, _ = module.source(request)
    page = Mock(url=address)
    monkeypatch.setattr(module, '_raise_if_login_required', lambda *args: None)
    monkeypatch.setattr(module, '_platform_challenge_visible', lambda *args: challenge)
    monkeypatch.setattr(module, '_extract_rows', lambda *args: [{}] if posts else [])
    result = module.review_page(page, request, tmp_path, action=action)
    assert result['ready'] == ready and result['needs_human_review']
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
    page.mouse.wheel.assert_not_called(); page.evaluate.assert_not_called()


def test_task_browser_attachment_never_closes_original(tmp_path, monkeypatch):
    context = Mock(pages=[Mock()]); context.pages[0].is_closed.return_value = False
    browser = Mock(contexts=[context]); playwright = Mock()
    playwright.chromium.connect_over_cdp.return_value = browser
    monkeypatch.setattr(discovery_profiles, 'owned_endpoint', lambda _: 'ws://127.0.0.1:1234/devtools/browser/owned')
    with discovery_profiles.persistent_page(playwright, tmp_path/'profile') as page:
        assert page is context.pages[0]
    browser.close.assert_not_called(); context.close.assert_not_called()
    playwright.chromium.launch_persistent_context.assert_not_called()


def test_reused_port_requires_matching_random_browser_id(tmp_path, monkeypatch):
    identifier = '/devtools/browser/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    (tmp_path/'DevToolsActivePort').write_text('12345\n'+identifier)
    client = Mock(); client.__enter__ = Mock(return_value=client); client.__exit__ = Mock(return_value=False)
    response = Mock(); client.get.return_value = response
    monkeypatch.setattr(discovery_profiles.httpx, 'Client', lambda **kwargs: client)
    response.json.return_value = {'webSocketDebuggerUrl': 'ws://127.0.0.1:12345'+identifier}
    assert discovery_profiles.owned_endpoint(tmp_path)
    response.json.return_value = {'webSocketDebuggerUrl': 'ws://127.0.0.1:12345/devtools/browser/other'}
    assert discovery_profiles.owned_endpoint(tmp_path) is None
    response.json.return_value = {'webSocketDebuggerUrl': 'ws://evil.test:12345'+identifier}
    assert discovery_profiles.owned_endpoint(tmp_path) is None
