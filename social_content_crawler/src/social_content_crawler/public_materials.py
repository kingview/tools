"""Phase-one public discovery. Never attaches to a signed-in browser."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal

from .browse_contracts import BrowsePlatform, BrowseSource, BrowseView
from .browse_backend import _extract_rows, build_source_url, normalize_rows
from .url_policy import PublicHttpsUrlPolicy
from .transfer_progress import report_transfer


class DiscoveryInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    platform: Literal['telegram', 'douyin', 'xiaohongshu']
    source: Literal['timeline', 'search', 'user', 'url'] = 'url'
    query: str | None = Field(default=None, max_length=300)
    user_key: str | None = Field(default=None, max_length=300)
    start_url: str | None = None
    sort: Literal['top', 'latest', 'likes'] = 'latest'
    media_type: Literal['both', 'image', 'video'] = 'both'
    max_items: int = Field(default=100, ge=1, le=500)
    days: int | None = Field(default=30, ge=1, le=36500)
    start_date: datetime | None = None
    end_date: datetime | None = None
    minimum_likes: int | None = Field(default=None, ge=0)
    minimum_views: int | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def validate_source(self):
        if self.platform == 'telegram':
            if self.source not in {'user', 'url'}:
                raise ValueError('Telegram 一期仅支持公开频道')
            telegram_address(self.start_url or self.user_key or '')
        elif self.source == 'url':
            parsed = urlsplit(self.start_url or '')
            domain = 'douyin.com' if self.platform == 'douyin' else 'xiaohongshu.com'
            if parsed.scheme != 'https' or parsed.username or parsed.password or not (parsed.hostname == domain or (parsed.hostname or '').endswith('.' + domain)):
                raise ValueError('来源网址与平台不匹配')
        if self.source == 'search' and not (self.query or '').strip():
            raise ValueError('请输入关键词')
        if self.source == 'user' and self.platform != 'telegram' and not re.fullmatch(r'[A-Za-z0-9_-]+', self.user_key or ''):
            raise ValueError('请提供明确账号 ID 或改用账号主页 URL；不能擅自选择同名账号')
        if self.start_date and self.end_date and utc(self.start_date) > utc(self.end_date):
            raise ValueError('开始时间不能晚于结束时间')
        return self


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def telegram_address(raw, *, message=False):
    raw = raw.strip()
    if re.fullmatch(r'@?[A-Za-z][A-Za-z0-9_]{3,63}', raw):
        raw = 'https://t.me/' + raw.lstrip('@')
    parsed = urlsplit(raw)
    match = re.fullmatch(r'/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,63})(?:/([1-9][0-9]*))?/?', parsed.path)
    if parsed.scheme != 'https' or parsed.hostname not in {'t.me', 'telegram.me'} or parsed.username or parsed.password or not match or match[1] in {'joinchat', 'addlist', 'share', 'proxy'}:
        raise ValueError('仅支持 Telegram 公开频道，不支持私密群、邀请或登录链接')
    if message and not match[2]:
        raise ValueError('下载需要具体消息 URL；频道链接请先使用链接发现')
    return match[1], match[2]


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


def export_links(posts, folder):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'links.txt').write_text('\n'.join(p['url'] for p in posts), encoding='utf-8')
    (folder / 'metadata.json').write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding='utf-8')
    with (folder / 'links.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['url','post_id','author_name','published_at','text'])
        writer.writeheader()
        writer.writerows({k:p.get(k) for k in writer.fieldnames} for p in posts)


def discover(request: DiscoveryInput, output_root: Path):
    from playwright.sync_api import sync_playwright
    selected, seen = [], set()
    warnings = []
    started = time.monotonic()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(30000)
            if request.platform == 'telegram':
                channel, _ = telegram_address(request.start_url or request.user_key or '')
                address = 'https://t.me/s/' + channel
                model = None
            else:
                view = BrowseView.LATEST if request.platform == 'xiaohongshu' and request.source == 'search' and request.sort == 'latest' else BrowseView.POSTS if request.source == 'user' else BrowseView.TOP
                model = SimpleNamespace(platform=BrowsePlatform(request.platform), source=BrowseSource(request.source),
                    view=view, query=request.query, user_key=request.user_key, start_url=request.start_url)
                address = build_source_url(model)
            page.goto(address, wait_until='domcontentloaded')
            stagnant = 0
            for _ in range(150):
                if time.monotonic()-started > 300:
                    warnings.append('发现阶段达到 5 分钟上限；保留已发现链接')
                    break
                page.wait_for_timeout(700)
                raw = telegram_rows(page) if model is None else [p.model_dump(mode='json') for p in normalize_rows(model.platform, _extract_rows(page, model), 500)]
                new = [p for p in raw if p['url'] not in seen]
                for post in new:
                    seen.add(post['url'])
                    if accepted(post, request):
                        selected.append(post)
                if len(selected) >= request.max_items:
                    break
                stagnant = 0 if new else stagnant + 1
                if stagnant >= 3:
                    break
                if model is None:
                    ids = [int(p['post_id']) for p in raw]
                    if not ids or min(ids) <= 1:
                        break
                    page.goto(address + '?before=' + str(min(ids)), wait_until='domcontentloaded')
                else:
                    page.mouse.wheel(0, 900)
            if request.sort == 'likes':
                selected.sort(key=lambda p:p.get('metrics', {}).get('likes') or 0, reverse=True)
                warnings.append('点赞排序依据本次可访问结果，不代表平台全量排名')
            elif request.sort == 'latest':
                selected.sort(key=lambda p:p.get('published_at') or '', reverse=True)
            selected = selected[:request.max_items]
            if len(selected) < request.max_items:
                warnings.append('符合条件的可访问结果不足；未知日期或不满足筛选的帖子不计入数量。登录或验证码限制请使用已注册窗口人工处理。')
            folder = output_root / 'discovered-links' / uuid.uuid4().hex
            export_links(selected, folder)
            return {'posts': selected, 'count': len(selected), 'requested': request.max_items,
                    'completed': len(selected) == request.max_items, 'warnings': warnings, 'output_directory': str(folder)}
        finally:
            browser.close()  # Only our anonymous browser, never a user's window.


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
