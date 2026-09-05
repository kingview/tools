"""Versioned browser lock identities, vendored into standalone social plugins.

Keep operation and workflow namespaces distinct; a subprocess must not acquire
its parent's workflow lock. Existing lock filenames are preserved on upgrade.
"""
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

WORKFLOW_LOCK_DIRECTORY = 'social-agent-workflow-leases'
OPERATION_LOCK_DIRECTORY = 'social-agent-profile-locks'


def api_port(api_url):
    parts = urlsplit(api_url)
    port = parts.port or 80
    if (parts.scheme!='http' or parts.hostname not in {'localhost','127.0.0.1','::1'}
            or parts.username or parts.password or parts.path not in {'','/'} or parts.query or parts.fragment):
        raise ValueError('浏览器调度只允许本机比特浏览器 API。')
    return port


def workflow_key(api_url,profile_id):
    return hashlib.sha256(f'loopback:{api_port(api_url)}|{profile_id}'.encode()).hexdigest()


def operation_key(api_url,profile_id):
    # Legacy operation locks hash the exact validated client API URL. Do not
    # rename them while older installed plugins may still be running.
    return hashlib.sha256(f'{api_url}|{profile_id}'.encode()).hexdigest()


def lock_paths(temporary_root,api_url,profile_id):
    root = Path(temporary_root)
    return (root/WORKFLOW_LOCK_DIRECTORY/(workflow_key(api_url,profile_id)+'.lock'),
            root/OPERATION_LOCK_DIRECTORY/(operation_key(api_url,profile_id)+'.lock'))
