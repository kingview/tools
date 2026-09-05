"""Phase-one link discovery; anonymous by default, optional explicit BitBrowser."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .url_policy import PublicHttpsUrlPolicy
from .transfer_progress import report_transfer


from .discovery_contract import DiscoveryInput, utc, telegram_address


def accepted(post, request, now=None):
    types = post.get('media_types', [])
    if not types or (request.media_type != 'both' and request.media_type not in types):
        return False
    now = now or datetime.now(timezone.utc)
    low = request.start_date or (now-timedelta(days=request.days) if request.days else None)
    high = request.end_date
    date_filter = low is not None or high is not None
    published = post.get('published_at')
    if not published and date_filter:
        return False  # Unknown dates cannot be represented as passing a date filter.
    if date_filter:
        try:
            date = utc(datetime.fromisoformat(str(published).replace('Z', '+00:00')))
        except ValueError:
            return False
        if (low and date < utc(low)) or date > utc(high or now):
            return False
    for name, threshold in [('likes', request.minimum_likes), ('views', request.minimum_views)]:
        value = post.get('metrics', {}).get(name)
        if threshold is not None and (value is None or value <= threshold):
            if request.platform == 'telegram' and value is None:
                continue
            return False
    return True


TG_ROWS = r'''nodes => nodes.map(n => ({
 url:'https://t.me/' + n.dataset.post, post_id:n.dataset.post.split('/').pop(),
 author_name:n.querySelector('.tgme_widget_message_author')?.textContent?.trim(),
 text:n.querySelector('.tgme_widget_message_text')?.innerText || '',
 published_at:n.querySelector('time')?.getAttribute('datetime'),
 view_text:n.querySelector('.tgme_widget_message_views')?.textContent,
 media:[...n.querySelectorAll('.tgme_widget_message_photo_wrap, video')].map(e => ({
  kind:e.tagName==='VIDEO'?'video':'image',
  url:e.tagName==='VIDEO'?(e.currentSrc || e.src || e.querySelector('source')?.src):
      (getComputedStyle(e).backgroundImage.match(/url\(["']?(.*?)["']?\)/)?.[1])
 })).filter(m => m.url)
}))'''


def telegram_rows(page):
    rows = page.locator('.tgme_widget_message[data-post]').evaluate_all(TG_ROWS)
    for row in rows:
        row['media_types'] = sorted({m['kind'] for m in row['media']})
        value = str(row.pop('view_text', '') or '').strip().upper()
        try:
            multiplier = 1000 if value.endswith('K') else 1000000 if value.endswith('M') else 1
            views = int(float(value.rstrip('KM')) * multiplier)
        except ValueError:
            views = None
        row['metrics'] = {'views': views}
    return rows


from .discovery_journal import export_links


def discover(request: DiscoveryInput, output_root: Path, registry=None):
    # Retain the public import path for existing plugin callers.
    from .material_discovery import discover as run
    return run(request, output_root, registry)


def download_telegram(url: str, output_root: Path, max_bytes=1000*1024*1024):
    from playwright.sync_api import sync_playwright
    channel, message = telegram_address(url, message=True)
    folder = output_root / 'telegram-public' / channel / message
    folder.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=True)
        try:
            page = browser.new_page()
            page.goto(f'https://t.me/s/{channel}/{message}', wait_until='domcontentloaded', timeout=30000)
            page.wait_for_timeout(500)
            posts = [p for p in telegram_rows(page) if p['post_id'] == message]
        finally:
            browser.close()
    if not posts or not posts[0]['media']:
        raise ValueError('公开页面没有暴露该消息的媒体；不切换到私人或登录会话')
    post, artifacts, total = posts[0], [], 0
    (folder / 'metadata.json').write_text(json.dumps(post, ensure_ascii=False, indent=2))
    (folder / 'text.txt').write_text(post.get('text') or '')
    policy = PublicHttpsUrlPolicy()
    with httpx.Client(timeout=httpx.Timeout(45, connect=20), follow_redirects=False) as client:
        for index, media in enumerate(post['media']):
            target = folder / f'{index+1:03d}{".mp4" if media["kind"] == "video" else ".jpg"}'
            receipt = target.with_suffix(target.suffix + '.json')
            if target.exists() and receipt.exists():
                previous = json.loads(receipt.read_text())
                with target.open('rb') as stream:
                    valid = hashlib.file_digest(stream, 'sha256').hexdigest() == previous['sha256']
                if valid:
                    total += target.stat().st_size
                    if total > max_bytes:
                        raise ValueError('已缓存的媒体超过本次下载大小上限；文件保留')
                    artifacts.append(previous)
                    continue
            media_url = media['url']
            for redirect in range(5):
                policy.validate(media_url, frozenset({'cdn-telegram.org', 'telegram.org', 't.me', 'telesco.pe'}))
                with client.stream('GET', media_url) as response:
                    if response.is_redirect:
                        from urllib.parse import urljoin
                        media_url = urljoin(media_url, response.headers['location'])
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get('content-type', '').split(';')[0]
                    if not content_type.startswith(('image/', 'video/', 'application/octet-stream')):
                        raise ValueError('服务器返回非媒体内容')
                    temporary, count, digest = target.with_suffix(target.suffix + '.part'), 0, hashlib.sha256()
                    with temporary.open('wb') as stream:
                        for chunk in response.iter_bytes(256*1024):
                            count += len(chunk)
                            if total + count > max_bytes:
                                raise ValueError('达到本次下载大小上限；保留已完成文件')
                            stream.write(chunk)
                            digest.update(chunk)
                            report_transfer({'filename':str(target),'downloaded_bytes':count,
                                'total_bytes':int(response.headers.get('content-length') or 0), 'status':'downloading'})
                    if not count:
                        raise ValueError('媒体文件为空')
                    temporary.replace(target)
                    artifact = {'path':str(target),'sha256':digest.hexdigest(),'size_bytes':count,'media_type':content_type}
                    receipt.write_text(json.dumps(artifact))
                    artifacts.append(artifact)
                    total += count
                    report_transfer({'filename':str(target),'downloaded_bytes':count,'status':'finished'})
                    break
            else:
                raise ValueError('媒体重定向次数过多')
    return {'artifacts':artifacts,'items':[post], 'completed':True, 'output_directory':str(folder)}
