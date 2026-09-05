from datetime import datetime, timedelta, timezone

import pytest
from social_content_crawler.public_materials import DiscoveryInput, accepted, telegram_address, export_links


def test_public_telegram_address_policy():
    assert telegram_address('https://t.me/example_channel/123',message=True)==('example_channel','123')
    for url in ['https://t.me/+invite','https://t.me/c/123/45','https://t.me/joinchat/invite','https://localhost/example_channel','https://t.me/example_channel']:
        with pytest.raises(ValueError): telegram_address(url,message=True)


def test_filter_media_time_engagement_and_absent_metric():
    now=datetime.now(timezone.utc)
    request=DiscoveryInput(platform='telegram',start_url='https://t.me/example_channel',max_items=10,minimum_likes=100)
    post={'media_types':['image'],'published_at':now.isoformat(),'metrics':{}}
    assert accepted(post,request,now)
    assert not accepted({**post,'media_types':[]},request,now)
    assert not accepted({**post,'published_at':(now-timedelta(days=31)).isoformat()},request,now)
    assert not accepted({**post,'published_at':None},request,now)
    xhs=DiscoveryInput(platform='xiaohongshu',source='search',query='科技',minimum_likes=100)
    assert not accepted(post,xhs,now)
    assert accepted({**post,'metrics':{'likes':101}},xhs,now)
    assert not accepted({**post,'metrics':{'likes':100}},xhs,now)


def test_inputs_limits_and_export(tmp_path):
    with pytest.raises(ValueError): DiscoveryInput(platform='telegram',source='search',query='anything')
    with pytest.raises(ValueError): DiscoveryInput(platform='telegram',start_url='https://t.me/example_channel',max_items=501)
    posts=[{'url':'https://t.me/example_channel/123','text':'文字','post_id':'123'}]
    export_links(posts,tmp_path)
    assert (tmp_path/'links.txt').read_text()==posts[0]['url']
    assert '文字' in (tmp_path/'metadata.json').read_text()
