from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Protocol

from .browse_contracts import BrowsePostsInput, BrowsePostsOutput
from .contracts import AuditEvent, ToolSpec
from .errors import CrawlerError, ErrorCode
from .ports import AuditSink, RateLimiter, ToolContext


BROWSE_TOOL_SPEC = ToolSpec(
    name="social.browse_posts",
    version="1.1.0",
    description=(
        "Browse an authorized Douyin, Xiaohongshu, X, or Telegram Web session and return "
        "structured post URLs and metadata."
    ),
    input_schema=BrowsePostsInput.model_json_schema(),
    output_schema=BrowsePostsOutput.model_json_schema(),
    category="read",
    side_effect=True,
    risk_level="medium",
    timeout_seconds=300,
    max_retries=1,
    idempotent=False,
    supports_dry_run=False,
    required_permissions=["social_content.read", "browser_session.use"],
    policy_tags=[
        "authorized-session-only",
        "read-only-browser-navigation",
        "opaque-session-reference",
        "audited",
    ],
    rate_limit_bucket="social-browser-read",
    requires_approval=False,
)


class BrowseBackend(Protocol):
    def run(self, request: BrowsePostsInput) -> BrowsePostsOutput: ...


class SocialPostBrowseTool:
    def __init__(
        self,
        *,
        backend: BrowseBackend,
        audit_sink: AuditSink,
        rate_limiter: RateLimiter,
    ) -> None:
        self._backend = backend
        self._audit_sink = audit_sink
        self._rate_limiter = rate_limiter

    @property
    def spec(self) -> ToolSpec:
        return BROWSE_TOOL_SPEC

    async def execute(
        self,
        request: BrowsePostsInput,
        context: ToolContext,
    ) -> BrowsePostsOutput:
        input_hash = _hash(request.model_dump_json(exclude_none=True))
        output: BrowsePostsOutput | None = None
        error: CrawlerError | None = None
        try:
            session_bucket = _hash(request.session_ref)[:16]
            await self._rate_limiter.acquire(
                f"{BROWSE_TOOL_SPEC.rate_limit_bucket}:{session_bucket}",
                context.tenant_id,
            )
            output = await asyncio.to_thread(self._backend.run, request)
            return output
        except CrawlerError as exc:
            error = exc
            raise
        except Exception as exc:
            error = CrawlerError(ErrorCode.BROWSE_FAILED, "unexpected browser failure")
            raise error from exc
        finally:
            await self._audit_sink.record(
                AuditEvent(
                    tenant_id=context.tenant_id,
                    trace_id=context.trace_id,
                    workflow_run_id=context.workflow_run_id,
                    agent_run_id=context.agent_run_id,
                    actor_type=context.actor_type,
                    actor_id=context.actor_id,
                    event_type="tool.failed" if error else "tool.succeeded",
                    tool_name=BROWSE_TOOL_SPEC.name,
                    tool_version=BROWSE_TOOL_SPEC.version,
                    input_hash=input_hash,
                    output_hash=(
                        _hash(output.model_dump_json(exclude_none=True)) if output else None
                    ),
                    error_code=error.code if error else None,
                    created_at=datetime.now(UTC),
                )
            )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
