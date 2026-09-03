from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from social_content_crawler import browser_lifecycle as lifecycle
from social_content_crawler.sessions import BitBrowserClient
from social_content_crawler.profile_tasks import ProfileTaskCoordinator


EXECUTION = "execution_12345678"
ENDPOINT = "http://127.0.0.1:9222"
API = "http://127.0.0.1:54345"
INSTANCE = "/devtools/browser/abcdefgh-12345678"


@pytest.fixture
def scope(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"execution_id": EXECUTION, "allowed_session_refs": ["test-session"]}))
    monkeypatch.setenv("SOCIAL_AGENT_EXECUTION_POLICY_PATH", str(policy))
    monkeypatch.setenv("SOCIAL_AGENT_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _: None)
    monkeypatch.setattr(lifecycle, "record_exception", Mock())
    return tmp_path


def snapshot(tabs, instance=INSTANCE):
    return {"instance": instance, "endpoint": ENDPOINT, "tabs": tabs}


def ledger(root, owned=True):
    path = lifecycle._directory(root, EXECUTION) / "profile.json"
    lifecycle._save(path, {"execution_id": EXECUTION, "api_url": API, "profile_id": "profile",
                          "instance": INSTANCE, "endpoint": ENDPOINT, "owned_window": owned,
                          "initial_tabs": ["existing"], "created_tabs": ["task-tab"], "cleaned": False})
    return path


def run_cleanup(root, monkeypatch, *, tabs=None, instance=INSTANCE, owned=True, endpoint=ENDPOINT):
    path = ledger(root, owned)
    client = Mock()
    client._running_profile_endpoint.return_value = endpoint
    monkeypatch.setattr(lifecycle, "_snapshot", Mock(side_effect=[
        snapshot(["existing", "task-tab"], instance), snapshot(tabs or ["existing"], instance)]))
    close_tabs = Mock(return_value=1)
    monkeypatch.setattr(lifecycle, "_close_tabs", close_tabs)
    coordinator = SimpleNamespace(hold=lambda *a, **kw: nullcontext())
    result = lifecycle.cleanup(root, EXECUTION, client_factory=lambda _: client, coordinator=coordinator)
    return result, client, close_tabs, path


def test_existing_window_and_original_tabs_are_preserved(scope, monkeypatch):
    result, client, close_tabs, path = run_cleanup(scope, monkeypatch, owned=False)
    close_tabs.assert_called_once_with(ENDPOINT, ["task-tab"])
    client.close_profile.assert_not_called()
    assert result == {"closed_tabs": 1, "closed_windows": 0, "warnings": []}
    assert json.loads(path.read_text())["cleaned"]


def test_task_opened_window_is_closed_and_cleanup_is_idempotent(scope, monkeypatch):
    result, client, close_tabs, path = run_cleanup(scope, monkeypatch)
    client.close_profile.assert_called_once_with("profile")
    assert result["closed_windows"] == 1
    result = lifecycle.cleanup(scope, EXECUTION, client_factory=lambda _: client)
    assert result["closed_windows"] == 0
    close_tabs.assert_called_once()


def test_user_opened_new_tab_keeps_window(scope, monkeypatch):
    result, client, close_tabs, _ = run_cleanup(scope, monkeypatch, tabs=["existing", "user-tab"])
    client.close_profile.assert_not_called()
    close_tabs.assert_called_once_with(ENDPOINT, ["task-tab"])
    assert result["warnings"]


def test_reopened_window_is_not_touched(scope, monkeypatch):
    result, client, close_tabs, _ = run_cleanup(scope, monkeypatch, instance="/devtools/browser/new-instance")
    client.close_profile.assert_not_called()
    close_tabs.assert_not_called()
    assert result["warnings"]


def test_closed_window_is_never_reopened(scope, monkeypatch):
    result, client, close_tabs, _ = run_cleanup(scope, monkeypatch, endpoint=None)
    client.open_profile.assert_not_called()
    client.close_profile.assert_not_called()
    close_tabs.assert_not_called()


def test_busy_profile_is_preserved(scope, monkeypatch):
    ledger(scope)
    client = Mock()
    coordinator = ProfileTaskCoordinator(scope / "locks")
    with coordinator.hold(API, "profile"):
        result = lifecycle.cleanup(scope, EXECUTION, client_factory=lambda _: client, coordinator=coordinator)
    client.close_profile.assert_not_called()
    client._running_profile_endpoint.assert_not_called()
    assert result["warnings"]


def test_recording_reuse_does_not_overwrite_first_ownership(scope, monkeypatch):
    monkeypatch.setattr(lifecycle, "_snapshot", lambda _: snapshot(["existing"]))
    client = SimpleNamespace(api_url=API)
    lifecycle.record_profile(client, "profile", ENDPOINT, opened=True)
    lifecycle.record_profile(client, "profile", ENDPOINT, opened=False)
    files = list(lifecycle._directory(scope, EXECUTION).glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["owned_window"] is True
    assert files[0].stat().st_mode & 0o777 == 0o600


def test_record_page_tracks_target_id_only_and_ignores_original(scope, monkeypatch):
    path = ledger(scope)
    monkeypatch.setattr(lifecycle, "_snapshot", lambda _: snapshot(["existing", "task-tab", "new-tab"]))
    session = Mock()
    page = Mock()
    page.context.new_cdp_session.return_value = session
    session.send.return_value = {"targetInfo": {"targetId": "new-tab"}}
    lifecycle.record_page(page, ENDPOINT)
    session.send.return_value = {"targetInfo": {"targetId": "existing"}}
    lifecycle.record_page(page, ENDPOINT)
    data = json.loads(path.read_text())
    assert data["created_tabs"] == ["task-tab", "new-tab"]
    assert "url" not in data and "cookies" not in data


def test_only_popup_with_matching_opener_is_claimed(scope, monkeypatch):
    recorder = Mock()
    monkeypatch.setattr(lifecycle, "record_page", recorder)
    opener, popup = Mock(), Mock()
    lifecycle.record_task_popup(popup, opener, ENDPOINT)
    recorder.assert_not_called()
    popup.opener.return_value = opener
    lifecycle.record_task_popup(popup, opener, ENDPOINT)
    recorder.assert_called_once_with(popup, ENDPOINT)


def test_absent_task_scope_disables_recording(scope, monkeypatch):
    monkeypatch.delenv("SOCIAL_AGENT_EXECUTION_POLICY_PATH")
    probe = Mock()
    monkeypatch.setattr(lifecycle, "_snapshot", probe)
    lifecycle.record_profile(SimpleNamespace(api_url=API), "profile", ENDPOINT, opened=True)
    probe.assert_not_called()


@pytest.mark.parametrize("status,expected", [({}, True), ({"profile": 1234}, False), ([], False)])
def test_open_profile_claims_window_only_when_known_closed(scope, monkeypatch, status, expected):
    recorder = Mock()
    monkeypatch.setattr(lifecycle, "record_profile", recorder)
    def transport(path, payload):
        if path == "/browser/pids/alive":
            return {"success": True, "data": status}
        if path == "/browser/ports":
            return {"success": True, "data": {"profile": 9222}}
        if path == "/browser/open":
            return {"success": True, "data": {"http": ENDPOINT}}
        raise AssertionError(path)
    client = BitBrowserClient(API, transport=transport)
    assert client.open_profile("profile") == ENDPOINT
    assert recorder.call_args.kwargs["opened"] is expected


def test_close_uses_non_destructive_official_endpoint():
    calls = []
    def transport(path, payload):
        calls.append((path, payload))
        return {"success": True}
    BitBrowserClient(API, transport=transport).close_profile("profile")
    assert calls == [("/browser/close", {"id": "profile"})]


def test_invalid_execution_id_is_rejected(scope):
    with pytest.raises(ValueError):
        lifecycle.cleanup(scope, "../other-task")


@pytest.mark.skipif(os.getenv("SOCIAL_AGENT_BROWSER_TESTS") != "1", reason="isolated CDP fixture")
def test_real_cdp_closes_only_recorded_tab(scope, monkeypatch):
    from playwright.sync_api import sync_playwright
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.exists():
        pytest.skip("local isolated Chrome fixture unavailable")
    profile = scope / "isolated-chrome"
    # Independent, temporary profile. Never connect to BitBrowser or a user account.
    process = subprocess.Popen([str(chrome), "--headless=new", "--remote-debugging-port=0",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        active = profile / "DevToolsActivePort"
        # Event.wait avoids the recording-delay monkeypatch on time.sleep.
        from threading import Event
        while not active.exists():
            assert process.poll() is None and time.monotonic() < deadline
            Event().wait(0.05)
        endpoint = f"http://127.0.0.1:{active.read_text().splitlines()[0]}"
        client = Mock(api_url=API)
        lifecycle.record_profile(client, "isolated", endpoint, opened=False)
        original = set(lifecycle._snapshot(endpoint)["tabs"])
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(endpoint)
            context = browser.contexts[0]
            lifecycle.new_task_page(context, endpoint)
            # A tab the user opened concurrently is intentionally not registered.
            context.new_page()
        before = set(lifecycle._snapshot(endpoint)["tabs"])
        assert len(before) == len(original) + 2
        client._running_profile_endpoint.return_value = endpoint
        result = lifecycle.cleanup(scope, EXECUTION, client_factory=lambda _: client,
            coordinator=SimpleNamespace(hold=lambda *a, **kw: nullcontext()))
        after = set(lifecycle._snapshot(endpoint)["tabs"])
        assert original <= after and len(after) == len(before) - 1
        assert result == {"closed_tabs": 1, "closed_windows": 0, "warnings": []}
        client.close_profile.assert_not_called()
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=10)
