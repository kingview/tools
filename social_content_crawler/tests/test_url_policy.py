from __future__ import annotations

import socket

import pytest

from social_content_crawler.errors import CrawlerError, ErrorCode
from social_content_crawler.url_policy import PublicHttpsUrlPolicy


def test_url_policy_rejects_domain_outside_allowlist() -> None:
    with pytest.raises(CrawlerError) as caught:
        PublicHttpsUrlPolicy().validate(
            "https://evil.example/video", frozenset({"allowed.example"})
        )
    assert caught.value.code is ErrorCode.PERMISSION_DENIED


def test_url_policy_rejects_private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(CrawlerError) as caught:
        PublicHttpsUrlPolicy().validate(
            "https://allowed.example/video", frozenset({"allowed.example"})
        )
    assert caught.value.code is ErrorCode.UNSAFE_URL

