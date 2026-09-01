from __future__ import annotations

import pytest
from pydantic import ValidationError

from social_content_crawler.browse_contracts import BrowsePostsInput


SESSION_REF = "sess_x_abcdefghijklmnopqrstuvwx"


def test_browse_contract_accepts_supported_sources() -> None:
    assert BrowsePostsInput(
        platform="x",
        session_ref=SESSION_REF,
        source="search",
        view="latest",
        query="local AI",
    ).query == "local AI"
    assert BrowsePostsInput(
        platform="x",
        session_ref=SESSION_REF,
        source="user",
        view="media",
        user_key="OpenAI",
    ).user_key == "OpenAI"
    assert BrowsePostsInput(
        platform="x",
        session_ref=SESSION_REF,
        source="timeline",
        view="latest",
    ).source == "timeline"
    assert BrowsePostsInput(
        platform="x",
        session_ref=SESSION_REF,
        source="url",
        view="latest",
        start_url="https://x.com/explore",
    ).start_url.host == "x.com"


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "search", "view": "latest"},
        {"source": "user", "view": "posts"},
        {"source": "timeline", "view": "media"},
        {"source": "url", "view": "latest", "start_url": "https://example.com"},
        {"source": "search", "view": "latest", "query": "x", "session_ref": "cookie=raw"},
    ],
)
def test_browse_contract_rejects_invalid_source_combinations(payload: dict) -> None:
    payload.setdefault("session_ref", SESSION_REF)
    payload.setdefault("platform", "x")
    with pytest.raises(ValidationError):
        BrowsePostsInput(**payload)


def test_mainland_platform_contracts_and_session_prefixes() -> None:
    douyin = BrowsePostsInput(
        platform="douyin",
        session_ref="sess_douyin_abcdefghijklmnopqrstuvwx",
        source="search",
        view="media",
        query="人工智能",
    )
    xhs = BrowsePostsInput(
        platform="xiaohongshu",
        session_ref="sess_xhs_abcdefghijklmnopqrstuvwx",
        source="user",
        view="posts",
        user_key="5f1234567890abcdef123456",
    )
    assert douyin.platform == "douyin"
    assert xhs.platform == "xiaohongshu"


def test_session_reference_must_match_platform() -> None:
    with pytest.raises(ValidationError):
        BrowsePostsInput(
            platform="douyin",
            session_ref=SESSION_REF,
            source="timeline",
            view="top",
        )


def test_telegram_contract_accepts_channel_and_rejects_search() -> None:
    request = BrowsePostsInput(
        platform="telegram",
        session_ref="sess_telegram_abcdefghijklmnopqrstuvwx",
        source="url",
        view="posts",
        start_url="https://t.me/weme_download",
    )
    assert request.platform == "telegram"
    with pytest.raises(ValidationError):
        BrowsePostsInput(
            platform="telegram",
            session_ref="sess_telegram_abcdefghijklmnopqrstuvwx",
            source="search",
            view="top",
            query="web3",
        )
