"""Canonical discovery contract; vendored unchanged into the social plugin."""
from datetime import datetime, time, timedelta, timezone
import re
from urllib.parse import urlsplit
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    browser_engine: Literal['standard', 'bitbrowser'] = 'standard'
    execution_mode: Literal['automation', 'rpa'] = 'automation'
    session_ref: str | None = None
    access_interval_seconds: float = Field(default=1, ge=.3, le=30)
    timeout_seconds: float = Field(default=300, ge=10, le=3600)

    @model_validator(mode='before')
    @classmethod
    def defaults(cls,value):
        if not isinstance(value,dict):
            return value
        value = dict(value)
        value.setdefault('source','url' if value.get('start_url') else default_source(value.get('platform')))
        value.setdefault('execution_mode',default_mode(value.get('browser_engine','standard')))
        value.setdefault('sort','latest' if value.get('platform')=='telegram' else 'top')
        return value

    @model_validator(mode='after')
    def validate_source(self):
        if self.browser_engine == 'bitbrowser':
            if self.platform == 'telegram':
                raise ValueError('Telegram 公开频道固定使用标准浏览器')
            prefix = 'xhs' if self.platform == 'xiaohongshu' else 'douyin'
            if not re.fullmatch(r'sess_'+prefix+r'_[A-Za-z0-9_-]{20,80}', self.session_ref or ''):
                raise ValueError('请选择对应平台的比特浏览器窗口')
        elif self.session_ref:
            raise ValueError('标准浏览器不能复用登录会话')
        if self.platform == 'telegram' and self.execution_mode != 'automation':
            raise ValueError('Telegram 公开频道固定使用高效模式')
        if self.platform == 'telegram':
            if self.source not in {'user', 'url'}:
                raise ValueError('Telegram 一期仅支持公开频道')
            _, message = telegram_address(self.start_url or self.user_key or '')
            if message:
                raise ValueError('链接发现请填写频道地址，具体消息地址请使用下载工具')
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

DISCOVERY_LIMITS = {
    name: (next(m.ge for m in DiscoveryInput.model_fields[name].metadata if hasattr(m,'ge')),
           next(m.le for m in DiscoveryInput.model_fields[name].metadata if hasattr(m,'le')))
    for name in ('max_items','access_interval_seconds','timeout_seconds')
}


def default_source(platform):
    return "url" if platform == "telegram" else "timeline"


def default_mode(browser_engine):
    return "rpa" if browser_engine == "bitbrowser" else "automation"


def calendar_bounds(start, end):
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    return {
        "start_date": datetime.combine(start,time.min).astimezone().isoformat(),
        "end_date": (datetime.combine(end+timedelta(days=1),time.min)-timedelta(microseconds=1)).astimezone().isoformat(),
    }
