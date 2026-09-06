"""One public media response -> a partial file; final commit belongs to cache."""
import hashlib
from urllib.parse import urljoin

import httpx

from .material_control import check_material_control
from .public_media_cache import media_type_allowed, temporary_path, validate_paths
from .transfer_progress import report_transfer
from .url_policy import PublicHttpsUrlPolicy

ALLOWED_HOSTS = frozenset({'cdn-telegram.org', 'telegram.org', 't.me', 'telesco.pe'})


async def fetch_media(client, media_url, target, remaining_bytes):
    """Borrow the caller's client; close every response, including redirects."""
    validate_paths(target)
    policy = PublicHttpsUrlPolicy()
    report_transfer({'filename': str(target), 'downloaded_bytes': 0, 'status': 'downloading'})
    try:
        for _ in range(5):
            check_material_control()
            policy.validate(media_url, ALLOWED_HOSTS)
            async with client.stream('GET', media_url) as response:
                if response.is_redirect:
                    media_url = urljoin(media_url, response.headers['location'])
                    continue
                response.raise_for_status()
                content_type = response.headers.get('content-type', '').split(';')[0]
                if not media_type_allowed(content_type):
                    raise ValueError('服务器返回非媒体内容')
                count, digest = 0, hashlib.sha256()
                with temporary_path(target).open('wb') as stream:
                    async for chunk in response.aiter_bytes(256 * 1024):
                        check_material_control()
                        count += len(chunk)
                        if count > remaining_bytes:
                            raise ValueError('达到本次下载大小上限；保留已完成文件')
                        stream.write(chunk)
                        digest.update(chunk)
                        report_transfer({'filename': str(target), 'downloaded_bytes': count,
                            'total_bytes': int(response.headers.get('content-length') or 0), 'status': 'downloading'})
                check_material_control()
                if not count:
                    raise ValueError('媒体文件为空')
                return {'sha256': digest.hexdigest(), 'size_bytes': count, 'media_type': content_type}
        raise ValueError('媒体重定向次数过多')
    except httpx.TimeoutException as exc:
        raise TimeoutError(f'{target.name} 下载等待超时；已完成文件保留，可继续任务') from exc
