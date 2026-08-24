from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from social_content_crawler.browse_contracts import BrowsePostsInput, BrowsePostsOutput
from social_content_crawler.browse_tool import BROWSE_TOOL_SPEC, SocialPostBrowseTool
from social_content_crawler.ports import ToolContext
from social_content_crawler.runtime import InMemoryAuditSink, LocalRateLimiter


SESSION_REF = "sess_x_abcdefghijklmnopqrstuvwx"


class FakeBrowseBackend:
    def run(self, request: BrowsePostsInput) -> BrowsePostsOutput:
        return BrowsePostsOutput(
            platform="x",
            source_url="https://x.com/search?q=test&src=typed_query&f=live",
            posts=[],
            truncated=False,
            warnings=["no posts in fixture"],
            observed_at=datetime.now(UTC),
        )


def test_browse_tool_executes_and_audits() -> None:
    audit = InMemoryAuditSink()
    tool = SocialPostBrowseTool(
        backend=FakeBrowseBackend(),
        audit_sink=audit,
        rate_limiter=LocalRateLimiter(minimum_interval_seconds=0),
    )
    output = asyncio.run(
        tool.execute(
            BrowsePostsInput(
                platform="x",
                session_ref=SESSION_REF,
                source="search",
                view="latest",
                query="test",
            ),
            ToolContext(
                tenant_id="tenant-1",
                trace_id="trace-1",
                actor_type="agent",
                actor_id="agent-1",
            ),
        )
    )

    assert output.platform == "x"
    assert audit.events[0].tool_name == "social.browse_posts"
    assert audit.events[0].tool_version == "1.0.0"
    assert audit.events[0].event_type == "tool.succeeded"
    assert BROWSE_TOOL_SPEC.required_permissions == [
        "social_content.read",
        "browser_session.use",
    ]
    assert BROWSE_TOOL_SPEC.input_schema["additionalProperties"] is False
