import hashlib
import json

import pytest

from social_content_crawler import public_media_cache as cache
from social_content_crawler.material_control import MaterialControlInterrupted


def legacy_file(tmp_path, content=b'old media'):
    target = tmp_path/'001.jpg'
    target.write_bytes(content)
    receipt = {'path': str(target), 'sha256': hashlib.sha256(content).hexdigest(),
               'size_bytes': len(content), 'media_type': 'image/jpeg'}
    cache.receipt_path(target).write_text(json.dumps(receipt))
    return target, receipt


@pytest.fixture(autouse=True)
def diagnostic_errors(monkeypatch):
    errors = []
    monkeypatch.setattr(cache, 'record_exception', lambda *args: errors.append(args))
    return errors


def test_legacy_receipt_remains_usable_without_trusting_saved_path(tmp_path):
    target, expected = legacy_file(tmp_path)
    assert cache.cached_artifact(target) == expected
    altered = {**expected, 'path': '/not-the-downloaded-file', 'extra': 'ignored'}
    cache.receipt_path(target).write_text(json.dumps(altered))
    assert cache.cached_artifact(target) == expected


@pytest.mark.parametrize('payload', ['{broken', '[]', '{}', 'null', '\ud800', 'x'*65537,
    json.dumps({'sha256': 'a'*64, 'size_bytes': True, 'media_type': 'image/jpeg'}),
    json.dumps({'sha256': 'a'*64, 'size_bytes': 8, 'media_type': 'text/html'}),
    json.dumps({'sha256': 'wrong', 'size_bytes': 8, 'media_type': 'image/jpeg'})])
def test_bad_receipt_is_logged_cache_miss_not_a_deleted_file(tmp_path, payload, diagnostic_errors):
    target, _ = legacy_file(tmp_path)
    cache.receipt_path(target).write_bytes(payload.encode('utf-8', errors='surrogatepass'))
    assert cache.cached_artifact(target) is None
    assert target.read_bytes() == b'old media'
    assert diagnostic_errors


@pytest.mark.parametrize('content', [b'', b'wrong', b'bad media'])
def test_cached_file_size_and_hash_are_both_verified(tmp_path, content):
    target, _ = legacy_file(tmp_path)
    target.write_bytes(content)
    assert cache.cached_artifact(target) is None
    assert target.read_bytes() == content


def test_missing_receipt_does_not_remove_old_file(tmp_path):
    target, _ = legacy_file(tmp_path)
    cache.receipt_path(target).unlink()
    assert cache.cached_artifact(target) is None
    assert target.exists()


def test_hashing_large_cache_can_be_interrupted_without_changing_files(tmp_path, monkeypatch):
    target, expected = legacy_file(tmp_path, b'x'*(3*1024*1024))
    calls = []
    def pause():
        calls.append(1)
        if len(calls) == 3:
            raise MaterialControlInterrupted('pause')
    monkeypatch.setattr(cache, 'check_material_control', pause)
    with pytest.raises(MaterialControlInterrupted):
        cache.cached_artifact(target)
    assert calls == [1, 1, 1]
    assert target.stat().st_size == expected['size_bytes']
    assert json.loads(cache.receipt_path(target).read_text()) == expected


def test_receipt_write_failure_is_not_success_and_next_attempt_revalidates(tmp_path, monkeypatch):
    target, old = legacy_file(tmp_path)
    cache.temporary_path(target).write_bytes(b'new media')
    def fail(*_):
        raise OSError('fixture disk failure')
    monkeypatch.setattr(cache, 'atomic_text', fail)
    with pytest.raises(OSError, match='disk failure'):
        cache.commit_download(target, sha256=hashlib.sha256(b'new media').hexdigest(),
                              size_bytes=9, media_type='image/jpeg')
    assert target.read_bytes() == b'new media'
    assert json.loads(cache.receipt_path(target).read_text()) == old
    assert cache.cached_artifact(target) is None


@pytest.mark.parametrize('name', ['001.jpg', '001.jpg.part', '001.jpg.json'])
def test_symlinks_are_not_followed_or_overwritten(tmp_path, name):
    target = tmp_path/'001.jpg'
    external = tmp_path/'external'
    external.write_bytes(b'preserve')
    (tmp_path/name).symlink_to(external)
    with pytest.raises(ValueError, match='普通文件'):
        cache.cached_artifact(target)
    assert external.read_bytes() == b'preserve'


def test_pause_before_commit_preserves_old_file_and_receipt(tmp_path, monkeypatch):
    target, old = legacy_file(tmp_path)
    cache.temporary_path(target).write_bytes(b'new media')
    def pause():
        raise MaterialControlInterrupted('pause')
    monkeypatch.setattr(cache, 'check_material_control', pause)
    with pytest.raises(MaterialControlInterrupted):
        cache.commit_download(target, sha256=hashlib.sha256(b'new media').hexdigest(),
                              size_bytes=9, media_type='image/jpeg')
    assert target.read_bytes() == b'old media'
    assert json.loads(cache.receipt_path(target).read_text()) == old
