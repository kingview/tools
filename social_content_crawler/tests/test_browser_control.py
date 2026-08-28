from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from social_content_crawler.browser_control import (
    BitBrowserControlBackend,
    _require_public_https_url,
)
from social_content_crawler.browser_control_contracts import (
    BrowserOperationInput,
    BrowserOperationOutput,
)
from social_content_crawler.browser_control_tool import (
    BROWSER_CONTROL_TOOL_SPEC,
    BitBrowserControlTool,
)
from social_content_crawler.errors import CrawlerError, ErrorCode
from social_content_crawler.ports import ToolContext
from social_content_crawler.runtime import InMemoryAuditSink, LocalRateLimiter


SESSION_REF = "sess_douyin_abcdefghijklmnopqrstuvwx"


def test_contract_supports_observe_click_input_scroll_and_paging() -> None:
    assert BrowserOperationInput(session_ref=SESSION_REF, action="observe").action == "observe"
    assert BrowserOperationInput(
        session_ref=SESSION_REF,
        action="click",
        element_ref="e3",
    ).element_ref == "e3"
    assert BrowserOperationInput(
        session_ref=SESSION_REF,
        action="input",
        role="searchbox",
        name="搜索",
        value="web3",
    ).value == "web3"
    assert BrowserOperationInput(
        session_ref=SESSION_REF,
        action="press",
        key="PageDown",
    ).key == "PageDown"
    assert BrowserOperationInput(
        session_ref=SESSION_REF,
        action="scroll",
        scroll_y=1200,
    ).scroll_y == 1200


def test_contract_rejects_ambiguous_targets_and_credentials_in_url() -> None:
    with pytest.raises(ValidationError, match="exactly one target"):
        BrowserOperationInput(
            session_ref=SESSION_REF,
            action="click",
            selector="button",
            text="下一页",
        )
    with pytest.raises(ValidationError, match="credential-free HTTPS"):
        BrowserOperationInput(
            session_ref=SESSION_REF,
            action="navigate",
            url="https://user:password@example.com/",
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:54345/",
        "https://10.0.0.3/",
        "https://localhost/",
        "https://service.local/",
    ],
)
def test_navigation_rejects_local_and_private_networks(url: str) -> None:
    with pytest.raises(CrawlerError) as raised:
        _require_public_https_url(url)
    assert raised.value.code is ErrorCode.INVALID_REQUEST


def test_backend_uses_session_ref_and_official_browser_open_endpoint() -> None:
    calls: list[tuple[str, str]] = []

    class FakeRegistry:
        def get(self, session_ref: str):
            assert session_ref == SESSION_REF
            return SimpleNamespace(api_url="http://127.0.0.1:54345", profile_id="profile-1")

    class FakeClient:
        def __init__(self, api_url: str):
            assert api_url == "http://127.0.0.1:54345"

        def open_profile(self, profile_id: str) -> str:
            assert profile_id == "profile-1"
            return "ws://127.0.0.1:50106/devtools/browser/test"

    class FakeAutomation:
        def perform(self, *, cdp_endpoint: str, request: BrowserOperationInput):
            calls.append((cdp_endpoint, request.action.value))
            return BrowserOperationOutput(
                action=request.action,
                url="https://www.douyin.com/",
                title="抖音",
            )

    backend = BitBrowserControlBackend(
        session_registry=FakeRegistry(),
        automation=FakeAutomation(),
        client_factory=FakeClient,
    )
    output = backend.run(BrowserOperationInput(session_ref=SESSION_REF, action="observe"))

    assert output.title == "抖音"
    assert calls == [("ws://127.0.0.1:50106/devtools/browser/test", "observe")]


def test_browser_control_tool_executes_and_audits() -> None:
    class FakeBackend:
        def run(self, request: BrowserOperationInput) -> BrowserOperationOutput:
            return BrowserOperationOutput(
                action=request.action,
                url="https://www.douyin.com/search/web3",
                title="web3 - 抖音搜索",
            )

    audit = InMemoryAuditSink()
    tool = BitBrowserControlTool(
        backend=FakeBackend(),
        audit_sink=audit,
        rate_limiter=LocalRateLimiter(minimum_interval_seconds=0),
    )
    output = asyncio.run(
        tool.execute(
            BrowserOperationInput(
                session_ref=SESSION_REF,
                action="navigate",
                url="https://www.douyin.com/search/web3",
            ),
            ToolContext(
                tenant_id="tenant-1",
                trace_id="trace-1",
                actor_type="agent",
                actor_id="agent-1",
            ),
        )
    )

    assert output.title == "web3 - 抖音搜索"
    assert audit.events[0].tool_name == "browser.operate"
    assert BROWSER_CONTROL_TOOL_SPEC.requires_approval is True

