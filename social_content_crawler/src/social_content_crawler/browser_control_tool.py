from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Protocol

from .browser_control_contracts import BrowserOperationInput, BrowserOperationOutput
from .contracts import AuditEvent, ToolSpec
from .errors import CrawlerError, ErrorCode
from .ports import AuditSink, RateLimiter, ToolContext


BROWSER_CONTROL_TOOL_SPEC = ToolSpec(
    name="browser.operate",
    version="1.0.0",
    description=(
        "Observe, navigate, click, input search text, press keys, and scroll in "
        "an authorized BitBrowser session without exposing credentials."
    ),
    input_schema=BrowserOperationInput.model_json_schema(),
    output_schema=BrowserOperationOutput.model_json_schema(),
    category="account_control",
    side_effect=True,
    risk_level="high",
    timeout_seconds=180,
    max_retries=0,
    idempotent=False,
    supports_dry_run=False,
    required_permissions=["social_content.read", "browser_session.use", "browser_ui.operate"],
    policy_tags=[
        "authorized-session-only",
        "no-credential-entry",
        "no-platform-write-actions",
        "opaque-session-reference",
        "audited",
    ],
    rate_limit_bucket="bitbrowser-ui-operation",
    requires_approval=True,
)


class BrowserControlBackend(Protocol):
    def run(self, request: BrowserOperationInput) -> BrowserOperationOutput: ...


class BitBrowserControlTool:
    def __init__(
        self,
        *,
        backend: BrowserControlBackend,
        audit_sink: AuditSink,
        rate_limiter: RateLimiter,
    ) -> None:
        self._backend = backend
        self._audit_sink = audit_sink
        self._rate_limiter = rate_limiter

    @property
    def spec(self) -> ToolSpec:
        return BROWSER_CONTROL_TOOL_SPEC

    async def execute(
        self,
        request: BrowserOperationInput,
        context: ToolContext,
    ) -> BrowserOperationOutput:
        input_hash = _hash(request.model_dump_json(exclude_none=True))
        output: BrowserOperationOutput | None = None
        error: CrawlerError | None = None
        try:
            session_bucket = _hash(request.session_ref)[:16]
            await self._rate_limiter.acquire(
                f"{BROWSER_CONTROL_TOOL_SPEC.rate_limit_bucket}:{session_bucket}",
                context.tenant_id,
            )
            output = await asyncio.to_thread(self._backend.run, request)
            return output
        except CrawlerError as exc:
            error = exc
            raise
        except Exception as exc:
            error = CrawlerError(ErrorCode.BROWSE_FAILED, "unexpected browser operation failure")
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
                    tool_name=BROWSER_CONTROL_TOOL_SPEC.name,
                    tool_version=BROWSER_CONTROL_TOOL_SPEC.version,
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
