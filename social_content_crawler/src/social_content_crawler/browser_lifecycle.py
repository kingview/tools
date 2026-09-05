"""Task-owned BitBrowser resources; no cleanup by URL or before/after diff.

Ownership is recorded only at explicit creation sites. The trusted local core
policy file supplies the task namespace (NOT model input/diagnostic metadata).
Cleanup is a core-invoked CLI, deliberately not exposed as an LLM tool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from playwright.sync_api import sync_playwright

from .diagnostics import record_exception


def _execution_scope():
    policy = os.getenv("SOCIAL_AGENT_EXECUTION_POLICY_PATH")
    root = os.getenv("SOCIAL_AGENT_STATE_ROOT")
    if not policy or not root:
        return None
    try:
        grant = json.loads(Path(policy).read_text())
        execution_id = grant.get("execution_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", execution_id):
            return None
        if not grant.get("allowed_session_refs"):
            return None
        return Path(root).resolve(), execution_id
    except (OSError, ValueError, TypeError):
        return None


def _directory(root: Path, execution_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", execution_id):
        raise ValueError("Invalid browser resource execution ID")
    return root / "browser-resources" / execution_id


def _base(endpoint: str) -> str:
    from .sessions import validate_loopback_cdp_endpoint
    parts = urlsplit(validate_loopback_cdp_endpoint(endpoint))
    return f"http://{parts.netloc}"


def _get_json(url: str):
    # Do not route loopback API/CDP probes through environment proxies.
    with build_opener(ProxyHandler({})).open(url, timeout=3) as response:
        return json.loads(response.read(1024 * 1024))


def _snapshot(endpoint: str) -> dict:
    base = _base(endpoint)
    version = _get_json(base + "/json/version")
    identity = urlsplit(version["webSocketDebuggerUrl"]).path
    if not re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]{8,100}", identity):
        raise ValueError("Unverifiable browser instance identity")
    targets = _get_json(base + "/json/list")
    return {"instance": identity, "endpoint": base,
            "tabs": [item["id"] for item in targets if item.get("type") == "page"]}


def _save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(payload, file, ensure_ascii=False)
    temporary.replace(path)


def record_profile(client, profile_id: str, endpoint: str, *, opened: bool) -> None:
    scope = _execution_scope()
    if scope is None:
        return
    try:
        if opened:
            time.sleep(0.5)  # Allow BitBrowser's saved tabs to restore.
        snapshot = _snapshot(endpoint)
        key = hashlib.sha256(f"{client.api_url}|{profile_id}|{snapshot['instance']}".encode()).hexdigest()
        path = _directory(*scope) / f"{key}.json"
        if path.exists():
            return  # Reusing the same window never replaces its original owner.
        _save(path, {"execution_id": scope[1], "api_url": client.api_url,
                     "profile_id": profile_id, "owned_window": opened,
                     "instance": snapshot["instance"], "endpoint": snapshot["endpoint"],
                     "initial_tabs": snapshot["tabs"], "created_tabs": [], "cleaned": False})
    except Exception as exc:
        record_exception("social-content", "browser_resources.record_profile", exc)


def record_page(page, endpoint: str) -> None:
    scope = _execution_scope()
    if scope is None:
        return
    try:
        snapshot = _snapshot(endpoint)
        directory = _directory(*scope)
        for path in directory.glob("*.json"):
            data = json.loads(path.read_text())
            if data["instance"] != snapshot["instance"] or data["endpoint"] != snapshot["endpoint"]:
                continue
            session = page.context.new_cdp_session(page)
            try:
                target = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
            finally:
                session.detach()
            if target not in data["initial_tabs"] and target not in data["created_tabs"]:
                data["created_tabs"].append(target)
                _save(path, data)
            return
    except Exception as exc:
        record_exception("social-content", "browser_resources.record_tab", exc)


def new_task_page(context, endpoint: str | None):
    page = context.new_page()
    if endpoint:
        record_page(page, endpoint)
    return page


def task_manages_pages() -> bool:
    """Keep intermediate pages for subsequent tools and failed-task inspection."""
    return _execution_scope() is not None


def preserve_for_review(endpoint: str) -> None:
    """Do not discard an explicitly identified environment awaiting the user."""
    scope = _execution_scope()
    if scope is None:
        return
    snapshot = _snapshot(endpoint)
    for path in _directory(*scope).glob('*.json'):
        data = json.loads(path.read_text())
        if data.get('instance') == snapshot['instance'] and data.get('endpoint') == snapshot['endpoint']:
            data['awaiting_human_review'] = True
            _save(path, data)


def record_task_popup(page, opener, endpoint: str) -> None:
    # Never claim an unrelated tab the user opened during an action.
    try:
        if _execution_scope() is not None and page.opener() == opener:
            record_page(page, endpoint)
    except Exception as exc:
        record_exception("social-content", "browser_resources.record_popup", exc)


def _close_tabs(endpoint: str, target_ids: list[str]) -> int:
    if not target_ids:
        return 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=5000)
        session = browser.new_browser_cdp_session()
        try:
            available = {item["targetId"] for item in session.send("Target.getTargets")["targetInfos"]
                         if item["type"] == "page"}
            return sum(bool(session.send("Target.closeTarget", {"targetId": target}).get("success"))
                       for target in target_ids if target in available)
        finally:
            session.detach()
        # Exiting Playwright only disconnects. Never call browser.close().


def cleanup(root: Path, execution_id: str, *, client_factory=None, coordinator=None) -> dict:
    from .sessions import BitBrowserClient, validate_loopback_api_url
    from .profile_tasks import GLOBAL_PROFILE_TASK_COORDINATOR
    client_factory = client_factory or (lambda url: BitBrowserClient(url, timeout_seconds=3))
    coordinator = coordinator or GLOBAL_PROFILE_TASK_COORDINATOR
    result = {"closed_tabs": 0, "closed_windows": 0, "warnings": []}
    for path in _directory(root, execution_id).glob("*.json"):
        try:
            data = json.loads(path.read_text())
            if data.get("execution_id") != execution_id or data.get("cleaned"):
                continue
            if data.get('awaiting_human_review'):
                result['warnings'].append('浏览器正在等待人工处理，已保留对应窗口和标签页。')
                continue
            client = client_factory(validate_loopback_api_url(data["api_url"]))
            with coordinator.hold(data["api_url"], data["profile_id"], timeout_seconds=0):
                endpoint = client._running_profile_endpoint(data["profile_id"])
                if not endpoint:
                    if not getattr(client, "_definitely_closed", False):
                        result["warnings"].append("无法确认浏览器窗口状态，已保留窗口。")
                    continue  # Closed/unreachable: never reopen for cleanup.
                current = _snapshot(endpoint)
                if current["instance"] != data["instance"]:
                    result["warnings"].append("浏览器窗口已被重新打开，保留新实例。")
                    continue
                result["closed_tabs"] += _close_tabs(endpoint, data["created_tabs"])
                remaining = _snapshot(endpoint)
                if data["owned_window"]:
                    if set(remaining["tabs"]) - set(data["initial_tabs"]) - set(data["created_tabs"]):
                        result["warnings"].append("任务新开的窗口中出现了未登记的标签页，已保留窗口。")
                        continue
                    # Recheck instance immediately before closing the profile.
                    if remaining["instance"] != data["instance"]:
                        continue
                    client.close_profile(data["profile_id"])
                    result["closed_windows"] += 1
                data["cleaned"] = True
                _save(path, data)
        except Exception as exc:
            record_exception("social-content", "browser_resources.cleanup", exc)
            result["warnings"].append("部分任务浏览器资源未能清理，已保留并记录日志。")
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--execution-id", required=True)
    args = parser.parse_args()
    print(json.dumps(cleanup(args.state_root.resolve(), args.execution_id), ensure_ascii=False))


if __name__ == "__main__":
    main()
