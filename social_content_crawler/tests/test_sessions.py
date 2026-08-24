from __future__ import annotations

import json
import os
from pathlib import Path
from stat import S_IMODE

import pytest

from social_content_crawler.errors import CrawlerError, ErrorCode
from social_content_crawler.sessions import (
    BitBrowserClient,
    SessionRegistry,
    validate_loopback_api_url,
)


PROFILE_ID = "profile-x-001"
AUTH_COOKIES = [
    {
        "domain": ".x.com",
        "name": "auth_token",
        "value": "secret-auth-token",
        "path": "/",
        "secure": True,
        "httpOnly": True,
    },
    {
        "domain": ".x.com",
        "name": "ct0",
        "value": "secret-csrf-token",
        "path": "/",
        "secure": True,
    },
    {
        "domain": ".example.com",
        "name": "unrelated",
        "value": "must-not-be-exported",
        "path": "/",
    },
]


def _transport(path: str, payload: dict):
    if path == "/health":
        return {"success": True, "data": None}
    if path == "/browser/list":
        return {
            "success": True,
            "data": {"list": [{"id": PROFILE_ID, "name": "X 工作账号 01"}]},
        }
    if path == "/browser/detail":
        assert payload == {"id": PROFILE_ID}
        return {
            "success": True,
            "data": {
                "id": PROFILE_ID,
                "name": "X 工作账号 01",
                "cookie": json.dumps(AUTH_COOKIES),
            },
        }
    if path == "/browser/open":
        assert payload == {"id": PROFILE_ID, "loadExtensions": False}
        return {
            "success": True,
            "data": {
                "ws": "ws://127.0.0.1:50106/devtools/browser/test",
                "http": "127.0.0.1:50106",
            },
        }
    raise AssertionError(path)


def _registry(tmp_path: Path) -> SessionRegistry:
    return SessionRegistry(
        tmp_path / "sessions.json",
        client_factory=lambda api_url: BitBrowserClient(api_url, transport=_transport),
    )


def test_bitbrowser_client_lists_profiles() -> None:
    client = BitBrowserClient("http://127.0.0.1:54345", transport=_transport)
    client.health()
    assert client.list_profiles()[0].profile_id == PROFILE_ID
    assert client.open_profile(PROFILE_ID).startswith("ws://127.0.0.1:50106/")


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com:54345",
        "http://127.0.0.1",
        "http://user:pass@127.0.0.1:54345",
        "http://127.0.0.1:54345/api",
    ],
)
def test_bitbrowser_api_must_be_loopback(value: str) -> None:
    with pytest.raises(CrawlerError) as raised:
        validate_loopback_api_url(value)
    assert raised.value.code is ErrorCode.INVALID_REQUEST


def test_registry_stores_only_opaque_profile_reference(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    record = registry.register_bitbrowser_x("http://127.0.0.1:54345", PROFILE_ID)

    assert record.session_ref.startswith("sess_x_")
    stored = registry.path.read_text(encoding="utf-8")
    assert "secret-auth-token" not in stored
    assert "secret-csrf-token" not in stored
    assert "cookie" not in stored.lower()
    if os.name != "nt":
        assert S_IMODE(registry.path.stat().st_mode) == 0o600


def test_session_cookiefile_is_x_only_and_always_deleted(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    record = registry.register_bitbrowser_x("http://127.0.0.1:54345", PROFILE_ID)

    cookiefile: Path | None = None
    with registry.materialize_cookiefile(
        record.session_ref,
        ["https://x.com/example/status/1"],
        tmp_path,
    ) as materialized:
        cookiefile = materialized
        text = materialized.read_text(encoding="utf-8")
        assert "secret-auth-token" in text
        assert "secret-csrf-token" in text
        assert "must-not-be-exported" not in text
        if os.name != "nt":
            assert S_IMODE(materialized.stat().st_mode) == 0o600
    assert cookiefile is not None and not cookiefile.exists()


def test_x_session_cannot_be_used_for_another_platform(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    record = registry.register_bitbrowser_x("http://127.0.0.1:54345", PROFILE_ID)
    with pytest.raises(CrawlerError) as raised:
        with registry.materialize_cookiefile(
            record.session_ref,
            ["https://www.youtube.com/watch?v=1"],
            tmp_path,
        ):
            pass
    assert raised.value.code is ErrorCode.INVALID_REQUEST


def test_registration_requires_a_logged_in_x_profile(tmp_path: Path) -> None:
    def anonymous_transport(path: str, payload: dict):
        response = _transport(path, payload)
        if path == "/browser/detail":
            response["data"]["cookie"] = "[]"
        return response

    registry = SessionRegistry(
        tmp_path / "sessions.json",
        client_factory=lambda api_url: BitBrowserClient(api_url, transport=anonymous_transport),
    )
    with pytest.raises(CrawlerError) as raised:
        registry.register_bitbrowser_x("http://127.0.0.1:54345", PROFILE_ID)
    assert raised.value.code is ErrorCode.SESSION_REAUTH_REQUIRED


@pytest.mark.parametrize(
    ("platform", "domain", "cookie_name", "prefix"),
    [
        ("douyin", ".douyin.com", "sessionid", "sess_douyin_"),
        ("xiaohongshu", ".xiaohongshu.com", "web_session", "sess_xhs_"),
    ],
)
def test_registers_mainland_platform_sessions(
    tmp_path: Path,
    platform: str,
    domain: str,
    cookie_name: str,
    prefix: str,
) -> None:
    def transport(path: str, payload: dict):
        if path == "/browser/detail":
            return {
                "success": True,
                "data": {
                    "id": PROFILE_ID,
                    "name": f"{platform}-profile",
                    "cookie": json.dumps(
                        [
                            {
                                "domain": domain,
                                "name": cookie_name,
                                "value": "platform-secret",
                                "path": "/",
                                "secure": True,
                            }
                        ]
                    ),
                },
            }
        raise AssertionError(path)

    registry = SessionRegistry(
        tmp_path / f"{platform}.sessions.json",
        client_factory=lambda api_url: BitBrowserClient(api_url, transport=transport),
    )
    record = registry.register_bitbrowser(platform, "http://127.0.0.1:54345", PROFILE_ID)

    assert record.session_ref.startswith(prefix)
    assert registry.validate_session(record.session_ref, platform) == record
    assert "platform-secret" not in registry.path.read_text(encoding="utf-8")
