import asyncio
import json

import httpx
import pytest

from social_content_crawler import public_media_transfer as transfer, material_http, public_media_fetch as fetch
from social_content_crawler.material_control import MaterialControlInterrupted
from social_content_crawler import public_media_cache as cache


def setup_client(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(transfer.httpx, 'AsyncClient',
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))
    validated = []
    monkeypatch.setattr(fetch.PublicHttpsUrlPolicy, 'validate',
        lambda self, url, allowed: validated.append(url))
    progress = []
    monkeypatch.setattr(transfer, 'report_transfer', progress.append)
    monkeypatch.setattr(fetch, 'report_transfer', progress.append)
    return validated, progress


@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_stop_retains_complete_file_and_resume_skips_it(tmp_path, monkeypatch, phase):
    interrupted = False
    cleaned, calls = [], []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal interrupted
            yield b'p' * 262144
            interrupted = True
            await asyncio.Event().wait()
        async def aclose(self):
            cleaned.append('body')

    async def handler(request):
        nonlocal interrupted
        calls.append(request.url.path)
        if request.url.path == '/1.jpg':
            return httpx.Response(200, content=b'completed', headers={'content-type': 'image/jpeg'})
        if phase == 'headers':
            interrupted = True
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append('headers')
        return httpx.Response(200, stream=Stream(), headers={'content-type': 'video/mp4'})

    validated, progress = setup_client(monkeypatch, handler)
    def check():
        if interrupted:
            raise MaterialControlInterrupted('stop')
    monkeypatch.setattr(material_http, 'check_material_control', check)
    post = {'media': [{'kind': 'image', 'url': 'https://cdn-telegram.org/1.jpg'},
                      {'kind': 'video', 'url': 'https://cdn-telegram.org/2.mp4'}]}
    with pytest.raises(MaterialControlInterrupted):
        transfer.save_media(post, tmp_path, 2**20)
    assert cleaned == [phase]
    assert (tmp_path/'001.jpg').read_bytes() == b'completed'
    receipt = json.loads((tmp_path/'001.jpg.json').read_text())
    assert receipt['size_bytes'] == 9
    assert not (tmp_path/'002.mp4').exists()
    if phase == 'body':
        assert (tmp_path/'002.mp4.part').stat().st_size == 262144
    assert progress[-1]['filename'].endswith('002.mp4')
    assert calls == ['/1.jpg', '/2.mp4']
    assert len(validated) == 2
    interrupted = False
    async def resumed(request):
        assert request.url.path == '/2.mp4'  # No duplicate download of first file.
        return httpx.Response(200, content=b'next', headers={'content-type': 'video/mp4'})
    # Restore the real factory before installing the resumed mock transport.
    monkeypatch.undo()
    setup_client(monkeypatch, resumed)
    result = transfer.save_media(post, tmp_path, 2**20)
    assert result['completed'] and len(result['artifacts']) == 2
    assert (tmp_path/'002.mp4').read_bytes() == b'next'
    assert not (tmp_path/'002.mp4.part').exists()


def test_redirects_revalidated_and_timeout_names_file(tmp_path, monkeypatch):
    def handler(request):
        if request.url.path == '/one':
            return httpx.Response(302, headers={'location': '/two'})
        raise httpx.ReadTimeout('fixture slow server', request=request)
    validated, progress = setup_client(monkeypatch, handler)
    with pytest.raises(TimeoutError, match='001.jpg.*超时.*可继续'):
        transfer.save_media({'media': [{'kind': 'image', 'url': 'https://cdn-telegram.org/one'}]}, tmp_path, 100)
    assert validated == ['https://cdn-telegram.org/one', 'https://cdn-telegram.org/two']
    assert progress[0]['downloaded_bytes'] == 0


@pytest.mark.parametrize('content_type,size,error', [('text/html', 1, '非媒体'),
                                                  ('image/jpeg', 101, '大小上限')])
def test_rejected_media_never_committed(tmp_path, monkeypatch, content_type, size, error):
    setup_client(monkeypatch, lambda request: httpx.Response(200, content=b'x'*size,
        headers={'content-type': content_type}))
    with pytest.raises(ValueError, match=error):
        transfer.save_media({'media': [{'kind': 'image', 'url': 'https://cdn-telegram.org/one'}]}, tmp_path, 100)
    assert not (tmp_path/'001.jpg').exists()
    assert not (tmp_path/'001.jpg.json').exists()


def test_corrupt_receipt_redownload_failure_preserves_original_until_success(tmp_path, monkeypatch):
    target = tmp_path/'001.jpg'
    target.write_bytes(b'old media')
    cache.receipt_path(target).write_text('{corrupt')
    monkeypatch.setattr(cache, 'record_exception', lambda *_: None)
    def fail(request):
        raise httpx.ReadTimeout('fixture timeout', request=request)
    setup_client(monkeypatch, fail)
    post = {'media': [{'kind': 'image', 'url': 'https://cdn-telegram.org/one'}]}
    with pytest.raises(TimeoutError):
        transfer.save_media(post, tmp_path, 100)
    assert target.read_bytes() == b'old media'
    monkeypatch.undo()
    monkeypatch.setattr(cache, 'record_exception', lambda *_: None)
    setup_client(monkeypatch, lambda request: httpx.Response(200, content=b'new media',
        headers={'content-type': 'image/jpeg'}))
    result = transfer.save_media(post, tmp_path, 100)
    assert target.read_bytes() == b'new media'
    assert cache.cached_artifact(target) == result['artifacts'][0]


def test_cached_files_count_toward_download_budget(tmp_path, monkeypatch):
    from test_public_media_cache import legacy_file
    target, receipt = legacy_file(tmp_path)
    setup_client(monkeypatch, lambda request: pytest.fail('cache must not trigger network'))
    with pytest.raises(ValueError, match='已缓存.*上限'):
        transfer.save_media({'media': [{'kind': 'image', 'url': 'https://cdn-telegram.org/one'}]}, tmp_path, 1)
    assert target.read_bytes() == b'old media'
    assert cache.cached_artifact(target) == receipt
