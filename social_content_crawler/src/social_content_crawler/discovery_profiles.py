"""Task-owned standard-browser profiles, kept outside exported material folders."""
from contextlib import contextmanager
import os
import re
from pathlib import Path

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
    context = playwright.chromium.launch_persistent_context(str(directory), channel='chrome', headless=False)
    try:
        page = next((page for page in context.pages if not page.is_closed()), None) or context.new_page()
        yield page
    finally:
        context.close()
