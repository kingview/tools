"""Coordinate cached files, request lifetime and per-post download budgets."""

import httpx

from .material_control import check_material_control
from .material_http import run_interruptible
from .public_media_cache import cached_artifact, commit_download
from .public_media_fetch import fetch_media
from .transfer_progress import report_transfer


def save_media(post, folder, max_bytes):
    return run_interruptible(lambda: _save_media(post, folder, max_bytes))


async def _save_media(post, folder, max_bytes):
    artifacts, total = [], 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=20), follow_redirects=False) as client:
        for index, media in enumerate(post['media']):
            check_material_control()
            target = folder / f'{index+1:03d}{".mp4" if media["kind"] == "video" else ".jpg"}'
            artifact = cached_artifact(target)
            if artifact is not None:
                total += artifact['size_bytes']
                if total > max_bytes:
                    raise ValueError('已缓存的媒体超过本次下载大小上限；文件保留')
            else:
                downloaded = await fetch_media(client, media['url'], target, max_bytes-total)
                artifact = commit_download(target, **downloaded)
                total += artifact['size_bytes']
            artifacts.append(artifact)
            report_transfer({'filename': str(target), 'downloaded_bytes': artifact['size_bytes'], 'status': 'finished'})
    return {'artifacts': artifacts, 'items': [post], 'completed': True, 'output_directory': str(folder)}
