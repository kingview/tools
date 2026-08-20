from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .contracts import AuditEvent, DownloadInput


@dataclass(frozen=True, slots=True)
class ToolContext:
    tenant_id: str
    trace_id: str
    actor_type: str
    actor_id: str
    workflow_run_id: str | None = None
    agent_run_id: str | None = None


class AuditSink(Protocol):
    async def record(self, event: AuditEvent) -> None: ...


class RateLimiter(Protocol):
    async def acquire(self, bucket: str, tenant_id: str) -> None: ...


class DownloaderBackend(Protocol):
    def run(self, request: DownloadInput, output_directory: Path) -> list[dict[str, Any]]: ...


class UrlPolicy(Protocol):
    def validate(self, url: str, allowed_domains: frozenset[str]) -> None: ...
