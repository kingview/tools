"""File/receipt integrity for public downloads; no network or browser ownership."""
import hashlib
import json
import re

from .diagnostics import record_exception
from .discovery_journal import atomic_text
from .material_control import check_material_control


def media_type_allowed(value):
    return isinstance(value, str) and (
        value.startswith(('image/', 'video/')) or value == 'application/octet-stream')


def receipt_path(target):
    return target.with_suffix(target.suffix + '.json')


def temporary_path(target):
    return target.with_suffix(target.suffix + '.part')


def validate_paths(target):
    # Never follow a partial file or receipt symlink outside the output folder.
    for path in (target, receipt_path(target), temporary_path(target)):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('下载目标或回执不是普通文件；原文件保留')


def cached_artifact(target):
    check_material_control()
    validate_paths(target)
    if not target.is_file() or not receipt_path(target).is_file():
        return None
    try:
        with receipt_path(target).open('rb') as stream:
            content = stream.read(65537)
        if len(content) > 65536:
            raise ValueError('媒体回执超过大小上限')
        receipt = json.loads(content)
        if (not isinstance(receipt, dict)
                or not isinstance(receipt.get('sha256'), str)
                or not re.fullmatch('[a-f0-9]{64}', receipt['sha256'])
                or type(receipt.get('size_bytes')) is not int or receipt['size_bytes'] <= 0
                or not media_type_allowed(receipt.get('media_type'))):
            raise ValueError('媒体回执字段不完整或无效')
    except (ValueError, UnicodeError) as exc:
        # A malformed receipt is a recoverable cache miss, not permission to
        # trust the existing file. I/O/permission failures still propagate.
        record_exception('social-content', 'public_media.cache_receipt', exc)
        return None
    if target.stat().st_size != receipt['size_bytes']:
        return None
    digest, size = hashlib.sha256(), 0
    with target.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            check_material_control()
            digest.update(chunk)
            size += len(chunk)
    check_material_control()
    if size != receipt['size_bytes'] or digest.hexdigest() != receipt['sha256']:
        return None
    # Only return the actual validated local file, never a path from receipt JSON.
    return {'path': str(target), 'sha256': digest.hexdigest(),
            'size_bytes': size, 'media_type': receipt['media_type']}


def commit_download(target, *, sha256, size_bytes, media_type):
    check_material_control()
    validate_paths(target)
    temporary_path(target).replace(target)
    artifact = {'path': str(target), 'sha256': sha256,
                'size_bytes': size_bytes, 'media_type': media_type}
    # Do not report success when the receipt could not be persisted. The file
    # remains for inspection and the next attempt revalidates its old receipt.
    atomic_text(receipt_path(target), json.dumps(artifact))
    return artifact
