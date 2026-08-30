from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from social_content_crawler.errors import CrawlerError, ErrorCode
from social_content_crawler.ports import ToolContext
from social_content_crawler.runtime import InMemoryAuditSink, LocalRateLimiter
from social_content_crawler.x_publish import XPublishBackend
from social_content_crawler.x_publish_contracts import XPublishInput, XPublishOutput
from social_content_crawler.x_publish_tool import X_PUBLISH_TOOL_SPEC, XPublishTool


SESSION_REF = "sess_x_abcdefghijklmnopqrstuvwx"
TOKEN = "approval-token-abcdefghijklmnopqrstuvwxyz"


def _request(**updates: object) -> XPublishInput:
    values = {
        "session_ref": SESSION_REF,
        "text": "Hello from Social Agent",
        "approval_token": TOKEN,
    }
    values.update(updates)
    return XPublishInput(**values)


def test_contract_requires_x_session_and_non_blank_text() -> None:
    assert _request().session_ref == SESSION_REF
    with pytest.raises(ValidationError):
        _request(session_ref="sess_douyin_abcdefghijklmnopqrstuvwx")
    with pytest.raises(ValidationError, match="blank"):
        _request(text="   ")


def test_backend_uses_x_profile_and_consumes_approval_once(tmp_path) -> None:
    media = tmp_path / "output" / "clip.mp4"
    media.parent.mkdir()
    media.write_bytes(b"video")
    calls: list[tuple[str, list[str]]] = []

    class FakeRegistry:
        def validate_x_session(self, session_ref: str):
            assert session_ref == SESSION_REF
            return SimpleNamespace(api_url="http://127.0.0.1:54345", profile_id="profile-x")

    class FakeClient:
        def __init__(self, api_url: str):
            assert api_url == "http://127.0.0.1:54345"

        def open_profile(self, profile_id: str) -> str:
            assert profile_id == "profile-x"
            return "ws://127.0.0.1:50106/devtools/browser/test"

    class FakeAutomation:
        def publish(self, *, cdp_endpoint, request, media_paths):
            calls.append((cdp_endpoint, [path.name for path in media_paths]))
            return XPublishOutput(
                state="published",
                post_url="https://x.com/i/status/123",
                text_length=len(request.text),
                media_count=len(media_paths),
            )

    backend = XPublishBackend(
        session_registry=FakeRegistry(),
        output_root=media.parent,
        expected_approval_token=TOKEN,
        automation=FakeAutomation(),
        client_factory=FakeClient,
    )
    output = backend.run(_request(media_paths=[str(media)]))

    assert output.state == "published"
    assert calls == [("ws://127.0.0.1:50106/devtools/browser/test", ["clip.mp4"])]
    with pytest.raises(CrawlerError) as raised:
        backend.run(_request())
    assert raised.value.code is ErrorCode.APPROVAL_REQUIRED


def test_backend_rejects_wrong_token_and_media_outside_output(tmp_path) -> None:
    class FakeRegistry:
        def validate_x_session(self, _session_ref: str):
            return SimpleNamespace(api_url="http://127.0.0.1:54345", profile_id="profile-x")

    class FakeClient:
        def __init__(self, _api_url: str):
            pass

        def open_profile(self, _profile_id: str) -> str:
            return "ws://127.0.0.1:50106/devtools/browser/test"

    class FakeAutomation:
        def publish(self, **_kwargs):
            raise AssertionError("must not publish")

    backend = XPublishBackend(
        session_registry=FakeRegistry(),
        output_root=tmp_path / "output",
        expected_approval_token=TOKEN,
        automation=FakeAutomation(),
        client_factory=FakeClient,
    )
    with pytest.raises(CrawlerError) as wrong:
        backend.run(_request(approval_token="wrong-token-that-is-long-enough-123456"))
    assert wrong.value.code is ErrorCode.APPROVAL_REQUIRED

    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"image")
    with pytest.raises(CrawlerError) as unsafe:
        backend.run(_request(media_paths=[str(outside)]))
    assert unsafe.value.code is ErrorCode.INVALID_REQUEST


def test_publish_tool_is_critical_non_retrying_and_audited() -> None:
    class FakeBackend:
        def run(self, request: XPublishInput) -> XPublishOutput:
            return XPublishOutput(
                state="published",
                post_url="https://x.com/i/status/123",
                text_length=len(request.text),
                media_count=0,
            )

    audit = InMemoryAuditSink()
    tool = XPublishTool(
        backend=FakeBackend(),
        audit_sink=audit,
        rate_limiter=LocalRateLimiter(minimum_interval_seconds=0),
    )
    output = asyncio.run(
        tool.execute(
            _request(),
            ToolContext(
                tenant_id="tenant-1",
                trace_id="trace-1",
                actor_type="agent",
                actor_id="agent-1",
            ),
        )
    )

    assert output.state == "published"
    assert X_PUBLISH_TOOL_SPEC.risk_level == "critical"
    assert X_PUBLISH_TOOL_SPEC.max_retries == 0
    assert audit.events[0].tool_name == "social.publish_x_post"

