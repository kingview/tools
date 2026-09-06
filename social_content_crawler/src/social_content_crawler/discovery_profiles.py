"""Task-owned standard-browser profiles, kept outside exported material folders."""
from contextlib import contextmanager
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .discovery_journal import atomic_text


def profile_directory(key, fingerprint):
    if not re.fullmatch('[a-f0-9]{32}',key) or not re.fullmatch('[a-f0-9]{64}',fingerprint):
        raise ValueError('无效浏览器任务标记')
    root = Path(os.environ.get('SOCIAL_AGENT_STATE_ROOT') or Path.home()/'.social-content-crawler').resolve()
    base = root/'discovery-browser-profiles'
    folder = base/key
    for directory in (base, folder):
        if directory.is_symlink(): raise ValueError('标准浏览器配置目录不能是符号链接')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    marker = folder/'request.sha256'
    if marker.is_symlink(): raise ValueError('浏览器任务标记无效')
    if marker.exists() and marker.read_text() != fingerprint:
        raise ValueError('浏览器环境与原任务不匹配，请新建任务')
    atomic_text(marker, fingerprint)
    return folder/'profile'


@contextmanager
def persistent_page(playwright, directory):
    # Chromium's own process lock rejects an environment already open in another
    # process. Never delete its SingletonLock or attach to an unrelated profile.
    if directory.is_symlink(): raise ValueError('浏览器配置目录不能是符号链接')
    directory.mkdir(mode=0o700, exist_ok=True); directory.chmod(0o700)
    endpoint = owned_endpoint(directory)
    if endpoint:
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=10000)
        if not browser.contexts:
            raise ValueError('任务浏览器没有可用页面，请重新打开原任务')
        context = browser.contexts[0]
        page = next((page for page in context.pages if not page.is_closed()), None) or context.new_page()
        yield page
        # This process only attached to this task's browser. Never close it.
        return
    context = playwright.chromium.launch_persistent_context(str(directory), channel='chrome', headless=False,
        args=['--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1'])
    try:
        page = next((page for page in context.pages if not page.is_closed()), None) or context.new_page()
        yield page
    finally:
        context.close()


def owned_endpoint(directory):
    """Attach only to the random debugger ID recorded in this task's profile.

    A stale/reused local port is not evidence of ownership. Never scan ports or
    read another browser's profile, and never remove Chromium's process lock.
    """
    marker = directory / 'DevToolsActivePort'
    if marker.is_symlink():
        raise ValueError('任务浏览器端口文件不能是符号链接')
    try:
        with marker.open() as stream:
            lines = stream.read(1025).splitlines()
        if len(lines) != 2 or not lines[0].isdigit() or not 1024 <= int(lines[0]) <= 65535:
            return None
        if not re.fullmatch(r'/devtools/browser/[a-fA-F0-9-]{20,80}', lines[1]):
            return None
        with httpx.Client(timeout=1, trust_env=False) as client:
            response = client.get(f'http://127.0.0.1:{int(lines[0])}/json/version')
            response.raise_for_status()
            endpoint = response.json().get('webSocketDebuggerUrl', '')
        parsed = urlsplit(endpoint)
        if (parsed.scheme == 'ws' and parsed.hostname in {'127.0.0.1', 'localhost'}
                and parsed.port == int(lines[0]) and parsed.path == lines[1]
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment):
            return endpoint
    except (OSError, ValueError, httpx.HTTPError, AttributeError):
        return None
    return None
