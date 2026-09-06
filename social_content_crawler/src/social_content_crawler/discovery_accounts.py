"""Account-name lookup through visible profile links, never a guessed ID."""
import re
from urllib.parse import quote, urlsplit, urlunsplit


def search_url(request):
    query = quote(request.user_key.strip(), safe='')
    if request.platform == 'xiaohongshu':
        return 'https://www.xiaohongshu.com/search_result?keyword='+query+'&type=51'
    return 'https://www.douyin.com/search/'+query+'?type=user'


def normalize_candidates(rows, platform):
    domain = 'xiaohongshu.com' if platform == 'xiaohongshu' else 'douyin.com'
    pattern = r'/user/profile/[A-Za-z0-9_-]+/?' if platform == 'xiaohongshu' else r'/user/[A-Za-z0-9_-]+/?'
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict): continue
        try:
            url = urlsplit(str(row.get('url', '')))
            port = url.port
        except ValueError:
            continue
        name = ' '.join(str(row.get('name', '')).split())[:200]
        if not name or url.scheme != 'https' or url.username or url.password or port not in (None, 443): continue
        if url.hostname not in (domain, 'www.'+domain) or not re.fullmatch(pattern, url.path): continue
        value = urlunsplit(('https', 'www.'+domain, url.path.rstrip('/'), '', ''))
        if value not in seen:
            seen.add(value); result.append({'name': name, 'url': value})
        if len(result) >= 30: break
    return result


def candidates(page, platform):
    selector = 'a[href*="/user/profile/"]' if platform == 'xiaohongshu' else 'a[href*="/user/"]'
    rows = page.locator(selector).evaluate_all('''elements => elements.filter(e => e.getClientRects().length)
        .slice(0,200).map(e => ({url:e.href,name:e.innerText || e.getAttribute('title') ||
            e.querySelector('img')?.alt || ''}))''')
    return normalize_candidates(rows, platform)
