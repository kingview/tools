from __future__ import annotations

from .diagnostics import logged

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Protocol

from .contracts import AuditEvent, ToolSpec
from .errors import CrawlerError, ErrorCode
from .ports import AuditSink, RateLimiter, ToolContext
from .x_publish_contracts import XPublishInput, XPublishOutput


X_PUBLISH_TOOL_SPEC = ToolSpec(
    name="social.publish_x_post",
    version="1.0.0",
    description=(
        "Publish exactly one approved X post through an already logged-in BitBrowser "
        "profile. This is an external write and must never be retried automatically."
    ),
    input_schema=XPublishInput.model_json_schema(),
    output_schema=XPublishOutput.model_json_schema(),
    category="external_write",
    side_effect=True,
    risk_level="critical",
    timeout_seconds=300,
    max_retries=0,
    idempotent=False,
    supports_dry_run=False,
    required_permissions=["social_content.write", "browser_session.use", "browser_ui.operate"],
    policy_tags=[
        "explicit-plan-approval",
        "one-time-authorization",
        "x-only",
        "no-automatic-retry",
        "audited",
    ],
    rate_limit_bucket="x-publish",
    requires_approval=True,
)


class XPublisherBackend(Protocol):
    def run(self, request: XPublishInput) -> XPublishOutput: ...


class XPublishTool:
    def __init__(
        self,
        *,
        backend: XPublisherBackend,
        audit_sink: AuditSink,
        rate_limiter: RateLimiter,
    ) -> None:
        self._backend = backend
        self._audit_sink = audit_sink
        self._rate_limiter = rate_limiter

    @property
    def spec(self) -> ToolSpec:
        return X_PUBLISH_TOOL_SPEC

    @logged("social-content", "social.publish_x_post")
    async def execute(self, request: XPublishInput, context: ToolContext) -> XPublishOutput:
        safe_input = request.model_dump(exclude={"approval_token"}, mode="json")
        input_hash = _hash(str(safe_input))
        output: XPublishOutput | None = None
        error: CrawlerError | None = None
        try:
            await self._rate_limiter.acquire(X_PUBLISH_TOOL_SPEC.rate_limit_bucket or "x-publish", context.tenant_id)
            output = await asyncio.to_thread(self._backend.run, request)
            return output
        except CrawlerError as exc:
            error = exc
            raise
        except Exception as exc:
            error = CrawlerError(ErrorCode.PUBLISH_FAILED, "unexpected X publication failure")
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
                    tool_name=X_PUBLISH_TOOL_SPEC.name,
                    tool_version=X_PUBLISH_TOOL_SPEC.version,
                    input_hash=input_hash,
                    output_hash=_hash(output.model_dump_json()) if output else None,
                    error_code=error.code if error else None,
                    created_at=datetime.now(UTC),
                )
            )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
