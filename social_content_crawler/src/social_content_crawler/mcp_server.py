from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .backend import YtDlpBackend
from .browse_backend import SocialPostBrowserBackend
from .browse_contracts import BrowsePostsInput
from .browse_tool import SocialPostBrowseTool
from .browser_control import BitBrowserControlBackend
from .browser_control_contracts import BrowserOperationInput
from .browser_control_tool import BitBrowserControlTool
from .contracts import DownloadInput
from .platforms import default_allowed_domains
from .ports import ToolContext
from .runtime import InMemoryAuditSink, LocalRateLimiter
from .sessions import SessionRegistry
from .tool import SocialMediaDownloadTool
from .url_policy import PublicHttpsUrlPolicy


mcp = FastMCP(
    "social-content",
    instructions=(
        "Read-only social browsing and local downloading. Never expose session "
        "cookies, proxy credentials, passwords, verification codes, or fingerprints."
    ),
)


class Runtime:
    def __init__(self) -> None:
        registry = SessionRegistry(_required_path("SOCIAL_AGENT_SESSION_REGISTRY", file=True))
        self.output_root = _required_path("SOCIAL_AGENT_OUTPUT_ROOT")
        audit = InMemoryAuditSink()
        limiter = LocalRateLimiter()
        self.browse = SocialPostBrowseTool(
            backend=SocialPostBrowserBackend(session_registry=registry),
            audit_sink=audit,
            rate_limiter=limiter,
        )
        self.browser = BitBrowserControlTool(
            backend=BitBrowserControlBackend(session_registry=registry),
            audit_sink=audit,
            rate_limiter=limiter,
        )
        self.download = SocialMediaDownloadTool(
            backend=YtDlpBackend(session_registry=registry),
            audit_sink=audit,
            rate_limiter=limiter,
            url_policy=PublicHttpsUrlPolicy(),
            output_root=self.output_root,
            allowed_domains=default_allowed_domains(),
        )

    @staticmethod
    def context() -> ToolContext:
        run_id = uuid.uuid4().hex
        return ToolContext(
            tenant_id="local-agent",
            trace_id=f"plugin-{run_id}",
            actor_type="agent",
            actor_id="social-content-plugin",
            agent_run_id=run_id,
        )


_runtime: Runtime | None = None


def runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime()
    return _runtime


@mcp.tool()
async def browse_posts(
    platform: str,
    session_ref: str,
    source: str = "search",
    view: str = "top",
    query: str | None = None,
    user_key: str | None = None,
    start_url: str | None = None,
    max_items: int = 20,
    max_scrolls: int = 8,
) -> dict[str, Any]:
    request = BrowsePostsInput(**locals())
    result = await runtime().browse.execute(request, runtime().context())
    return result.model_dump(mode="json")


@mcp.tool()
async def browser_operate(
    session_ref: str,
    action: str,
    url: str | None = None,
    element_ref: str | None = None,
    selector: str | None = None,
    role: str | None = None,
    name: str | None = None,
    text: str | None = None,
    value: str | None = None,
    key: str | None = None,
    scroll_y: int = 900,
    timeout_seconds: float = 30.0,
    wait_after_ms: int = 600,
    max_elements: int = 40,
    text_excerpt_chars: int = 4_000,
) -> dict[str, Any]:
    request = BrowserOperationInput(**locals())
    result = await runtime().browser.execute(request, runtime().context())
    return result.model_dump(mode="json")


@mcp.tool()
async def download_media(
    urls: list[str],
    session_ref: str,
    media_format: str = "best",
    max_total_size_mb: int = 1000,
) -> dict[str, Any]:
    request = DownloadInput(
        urls=urls,
        session_ref=session_ref,
        media_format=media_format,
        max_items=min(len(urls), 20),
        max_total_size_mb=max_total_size_mb,
    )
    result = await runtime().download.execute(request, runtime().context())
    return result.model_dump(mode="json")


def _required_path(name: str, *, file: bool = False) -> Path:
    raw = os.getenv(name, "").strip()
    if not raw:
        raise RuntimeError(f"{name} is required")
    path = Path(raw).expanduser().resolve()
    if file:
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
